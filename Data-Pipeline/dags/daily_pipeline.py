"""Daily pipeline: ingestion, processing and validation, one task per module (#22).

Triggered by hand until the evening run starts on the VM (#39). Params, both
inclusive: start (2015-01-01 or later) and end (empty: through the latest
data). The defaults are the full build, written to Data-Pipeline/data/interim/
and processed/. Any other range is a slice, written to
Data-Pipeline/data/slices/<start>_<end>/. Every run reads Data-Pipeline/data/raw/.

Seven tasks run pipeline scripts. The others are placeholders: they log the
issue that builds them and do nothing else.
"""

import logging

import pendulum
from airflow.sdk import Param, dag, get_current_context, task

import pipeline_steps as steps  # Data-Pipeline/scripts, on PYTHONPATH (docker-compose.yaml)

log = logging.getLogger("airflow.task")


def run_range() -> steps.DateRange:
    """The run's date range. Conf keys are checked too, so a misspelled key fails the run."""
    context = get_current_context()
    conf = context["dag_run"].conf or {}
    return steps.DateRange.from_params({**context["params"], **conf})


@dag(
    dag_id="daily_pipeline",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
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

    @task
    def run(step: str) -> None:
        steps.run_step(step, run_range())

    @task
    def planned(issue: str) -> None:
        log.warning("Placeholder until %s: nothing ran for %s", issue, run_range().label)

    def real(step: str):
        return run.override(task_id=step, doc_md=f"Runs {steps.SCRIPTS[step]}.py.")(step)

    def todo(step: str):
        issue = steps.PLANNED[step]
        return planned.override(task_id=step, doc_md=f"Placeholder until {issue}.")(issue)

    ingest = [
        todo(step)
        for step in (
            "ingest_legacy_311",
            "ingest_new_311",
            "ingest_noaa_logan",
            "ingest_nws_forecasts",
            "ingest_nws_alerts",
            "ingest_tides",
            "ingest_open_meteo",
        )
    ]
    static = real("ingest_static_layers")
    raw_checked = real("validate_raw")
    combined = real("combine_311")
    validated = real("validate_processed")

    check_range() >> [*ingest, static] >> raw_checked
    raw_checked >> [real("parse_legacy_311"), real("parse_new_311")] >> combined
    [combined, static] >> real("spatial_join") >> todo("daily_counts") >> todo("labels_and_features") >> validated
    validated >> [todo("schema_and_statistics"), todo("bias_slicing")]


daily_pipeline()
