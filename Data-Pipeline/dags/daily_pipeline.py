"""Daily pipeline: ingestion, processing and validation, one task per module (#22).

Triggered by hand until the evening run starts on the VM (#39). Params, both
inclusive: start (2015-01-01 or later) and end (empty: through the latest
data). The defaults are the full build, written to Data-Pipeline/data/interim/
and processed/. Any other range is a slice, written to
Data-Pipeline/data/slices/<start>_<end>/. Every run reads Data-Pipeline/data/raw/.

Sixteen tasks run pipeline scripts: five of them the weather and tide
downloads (#7, and Open-Meteo from #8) and three the alert history, labels and
features (#16). The others are placeholders: they log the issue that builds
them and do nothing else; an ingest task becomes real once its script is in
pipeline_steps.SCRIPTS. The last task, push_data, pushes
the DVC-tracked data to the team remote (dvc_push.py, #19); without bucket
access it logs `dvc push skipped: ...` and succeeds.

Every task logs its run id, rows in and rows out (#36). The ingest tasks retry
twice, about 5 to 10 minutes and then 10 to 15 minutes apart (exponential
backoff with a random spread); the others fail at once. A missing or empty
input fails at once even in an ingest task, since a retry cannot fix it. A
task that fails for good sends the W3-3 alert (alerts.py), which goes to its
log until #34 adds Slack or email.
"""

import logging

import pendulum
from airflow.sdk import Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

import alerts  # Data-Pipeline/scripts, on PYTHONPATH (docker-compose.yaml)
import dvc_push
import pipeline_steps as steps

log = logging.getLogger("airflow.task")


def run_range() -> steps.DateRange:
    """The run's date range. Conf keys are checked too, so a misspelled key fails the run."""
    context = get_current_context()
    conf = context["dag_run"].conf or {}
    return steps.DateRange.from_params({**context["params"], **conf})


def run_id() -> str:
    return get_current_context()["run_id"]


def retries(step: str) -> dict:
    """Retry settings for a task: only the ones that fetch from the network retry."""
    if step not in steps.NETWORK:
        return {}
    return {
        "retries": steps.RETRIES,
        "retry_delay": steps.RETRY_DELAY,
        "retry_exponential_backoff": steps.RETRY_BACKOFF,
        "max_retry_delay": steps.MAX_RETRY_DELAY,
    }


@dag(
    dag_id="daily_pipeline",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"on_failure_callback": alerts.on_task_failure},
    tags=["data-pipeline"],
    doc_md=__doc__,
    params={
        "start": Param(
            steps.FIRST_DAY.isoformat(),
            type="string",
            format="date",
            title="Start date",
            description="First day, inclusive. 2015-01-01 or later.",
        ),
        "end": Param(
            None,
            type=["null", "string"],
            format="date",
            title="End date",
            description="Last day, inclusive. Empty: through the latest data.",
        ),
    },
)
def daily_pipeline():
    @task
    def check_range() -> None:
        days = run_range()
        kind = "full build" if days.is_full else "slice"
        log.info(
            "%s %s: legacy years %s, outputs in %s",
            kind, days.label, days.legacy_years, steps.run_paths(days).interim.parent,
        )
        steps.log_task(run_id(), "check_range", None, None)

    @task
    def run(step: str) -> None:
        try:
            steps.run_step(step, run_range(), run_id=run_id())
        except steps.InputError as e:
            # A missing or empty input stays missing: fail now instead of retrying.
            raise AirflowFailException(str(e)) from e

    @task
    def push_data() -> None:
        # Needs .git, .dvc and .dvcignore mounted at the repo root (docker-compose.yaml).
        # dvc_push logs the push or the skip; a skip still succeeds.
        dvc_push.push(steps.REPO)
        steps.log_task(run_id(), "push_data", None, None)

    @task
    def planned(issue: str) -> None:
        log.warning("Placeholder until %s: nothing ran for %s", issue, run_range().label)
        steps.log_task(run_id(), get_current_context()["ti"].task_id, 0, 0)

    def real(step: str):
        return run.override(task_id=step, doc_md=f"Runs {steps.SCRIPTS[step]}.py.", **retries(step))(step)

    def todo(step: str):
        issue = steps.PLANNED[step]
        return planned.override(task_id=step, doc_md=f"Placeholder until {issue}.", **retries(step))(issue)

    ingest = {
        step: real(step) if step in steps.SCRIPTS else todo(step)
        for step in (
            "ingest_legacy_311",
            "ingest_new_311",
            "ingest_noaa_logan",
            "ingest_nws_forecasts",
            "ingest_nws_alerts",
            "ingest_tides",
            "ingest_open_meteo",
        )
    }
    # One after the other, legacy first: both write the 311 schedule state (#6).
    ingest["ingest_legacy_311"] >> ingest["ingest_new_311"]
    static = real("ingest_static_layers")
    alert_history = real("ingest_alert_history")
    raw_checked = real("validate_raw")
    combined = real("combine_311")
    counts = real("daily_counts")
    validated = real("validate_processed")

    check_range() >> [*ingest.values(), static, alert_history] >> raw_checked
    raw_checked >> [real("parse_legacy_311"), real("parse_new_311")] >> combined
    [combined, static] >> real("spatial_join") >> counts
    counts >> [real("surge_labels"), real("feature_table")] >> validated
    validated >> [todo("schema_and_statistics"), todo("bias_slicing")] >> push_data()


daily_pipeline()
