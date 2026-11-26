"""Steps of the daily_pipeline DAG (#22): date range, run folders and script arguments.

The DAG (Data-Pipeline/dags/daily_pipeline.py) imports this module through
PYTHONPATH (docker-compose.yaml, ADR-017), and the tests import it through
conftest.py. It uses the standard library only, so Airflow parses the DAG
without loading pandas or geopandas.

A real step calls a pipeline script's own main() with the arguments it would
get on the command line (run_script), so the scripts stay unchanged. Each real
step is one block in step_plan: its arguments, the files behind them and what
to do when an input is missing. Every other task is a placeholder until the
issue in PLANNED builds it.

The dvc.yaml stages (#18, ADR-024) run the same steps: each stage is
`python scripts/pipeline_steps.py <step>` (main below), which calls run_step
for the full build just as the DAG task does, and dvc_stages.py writes the
stages' deps and outs from step_plan. So a step's script, arguments and files
are defined once, here.

A slice keeps only its days, in Boston local time (new-system times are
already local, ADR-020): legacy rows right after parsing, and the combined
table after combining (TRIM). The new-system export stays whole, because
map_311 needs every new case to set the switch dates and to drop legacy
copies resubmitted to the new system. So a slice's combined table holds the
same rows as the full build has for those days.

Logging and failures (#36): every task logs one line with its run id, rows
in and rows out (log_task). A real step fails before its script runs when an
input file is missing or empty (InputError), and the DAG fails such a task
without retrying, since a retry cannot fix it. A step's outputs are deleted
before it runs, so a file left by an earlier run cannot pass for new output,
and empty files in a download folder are deleted so the download runs again.
The 311 ingest steps keep their outputs instead (keep_outputs): they are the
current downloads, which ingest_311.py replaces only when a source changed.
A script error is logged with the run and the task before it is raised again
unchanged. Tasks in NETWORK retry; the others fail at once, and their failure
alert comes from alerts.py.

Speed (#37): the DAG parses the legacy years side by side, one parse_legacy_311
task per year and at most LEGACY_PARALLEL at once, then merge_legacy_311 joins
them with legacy_311.read_years' rule, so the joined table is the same as one
parse of all the years.

The as-of date (#160): every step of a run reads one date, DateRange.as_of, the
Boston date the run forecasts from (tomorrow is as_of + 1). The DAG fixes it
from the run's run_after, the moment the run was triggered or scheduled, so a
run that starts at 18:05 and finishes after UTC midnight keeps one date; conf
can pass another. The downloads, the feature readers and the processed checks
take it as --as-of instead of reading the clock; a DVC stage needs --as-of or
BRT_AS_OF. A run is an evening run when it was triggered from that date's
forecast cutoff (18:00 Boston, labels_features.csv) up to 00:00 Boston on the
forecast day: only then does ingest_alert_history check the pull against the
NWS snapshot (ADR-014), ingest_open_meteo store the evening GFS answer and
forecast_rows write tomorrow's rows. A run for an earlier as-of date, by hand
days later or just after midnight, is not one: its snapshot and answers would
come from after that evening.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import logging
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]  # /opt/airflow/repo in the Airflow containers
FIRST_DAY = date(2015, 1, 1)  # first day of the training years (train 2015 to 2023)
LEGACY_YEARS = range(2015, 2027)  # yearly legacy 311 files, as in legacy_311.py
PARAMS = ("start", "end", "as_of")
BOSTON = ZoneInfo("America/New_York")

log = logging.getLogger(__name__)

# Task id -> pipeline script in Data-Pipeline/scripts.
SCRIPTS = {
    "schema_and_statistics": "schema_stats",
    "ingest_static_layers": "static_layers",
    "parse_legacy_311": "legacy_311",
    "parse_new_311": "new_311",
    "combine_311": "map_311",
    "spatial_join": "spatial_join",
    "daily_counts": "daily_counts",
    "ingest_alert_history": "alert_history",
    "surge_labels": "surge_labels",
    "feature_table": "feature_table",
    "bias_slicing": "bias_slicing",
    "validate_raw": "validate_data",
    "validate_processed": "validate_data",
    "ingest_noaa_logan": "noaa_logan",
    "ingest_nws_forecasts": "nws",
    "ingest_nws_alerts": "nws",
    "ingest_tides": "tides",
    "ingest_open_meteo": "open_meteo",
    "ingest_legacy_311": "ingest_311",
    "ingest_new_311": "ingest_311",
    "forecast_rows": "forecast_rows",
}
# Task id -> the issue that builds it; a placeholder in the DAG until then.
PLANNED: dict[str, str] = {}
# Task id -> the outputs a slice cuts to its days afterwards, with their time column.
# parse_legacy_311 is only cut when it parses the whole range in one go; per year,
# the cut waits for merge_legacy_311, so cross-year repeats are judged as before.
TRIM = {
    "parse_legacy_311": [("legacy_311.parquet", "open_dt")],
    "merge_legacy_311": [("legacy_311.parquet", "open_dt")],
    "combine_311": [("311_combined.parquet", "open_dt")],
}

# Tasks that fetch from the network retry twice. Airflow's exponential backoff
# adds a random spread, so the waits are about 5 to 10 minutes and then 10 to 15
# (capped). The placeholders are listed too, so #8 gets the same retries when
# it fills its placeholder.
NETWORK = {task for task in (*SCRIPTS, *PLANNED) if task.startswith("ingest_")}
RETRIES = 2
RETRY_DELAY = timedelta(minutes=5)
RETRY_BACKOFF = 2.0  # each retry's base wait is twice the one before
MAX_RETRY_DELAY = timedelta(minutes=15)


def legacy_parallel(value: str | None) -> int:
    """How many legacy years the DAG parses at once (#37): LEGACY_PARALLEL from the
    environment, 4 when it is unset. Anything other than a whole number of 1 or
    more stops the DAG from importing, with this error in list-import-errors."""
    if value is None or not value.strip():
        return 4
    try:
        parallel = int(value)
    except ValueError:
        parallel = 0
    if parallel < 1:
        raise ValueError(f"LEGACY_PARALLEL must be a whole number of 1 or more, got {value!r}")
    return parallel


# docker-compose.yaml passes LEGACY_PARALLEL from .env, so the VM can parse 2 years
# at once if memory is tight. Airflow's parallelism there (4) caps it anyway.
LEGACY_PARALLEL = legacy_parallel(os.environ.get("LEGACY_PARALLEL"))


class InputError(Exception):
    """A step's input file is missing or empty, so the step stops before its script runs."""


def parse_day(value, name: str) -> date | None:
    """A date param: None or "" means unset, otherwise an ISO date such as 2025-01-31."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a date like 2025-01-31, got {value!r}") from None


def boston_date(moment: datetime) -> date:
    """The Boston date of a moment, which must carry its time zone."""
    if moment.tzinfo is None:
        raise ValueError(f"{moment} has no time zone")
    return moment.astimezone(BOSTON).date()


def forecast_cutoff(as_of: date, root: Path = REPO) -> datetime:
    """The forecast cutoff on the evening of as_of: forecast_cutoff_local in labels_features.csv, Boston time."""
    settings = root / "Data-Pipeline" / "config" / "labels_features.csv"
    with settings.open(newline="", encoding="utf-8-sig") as f:
        value = next((row["value"].strip() for row in csv.DictReader(f) if row["setting"].strip() == "forecast_cutoff_local"), None)
    if value is None:
        raise ValueError(f"{settings} has no forecast_cutoff_local row")
    return datetime.combine(as_of, clock_time.fromisoformat(value), BOSTON)


def is_evening(run_after: datetime, as_of: date, root: Path = REPO) -> bool:
    """Whether a run triggered at run_after is the evening run of as_of: from its cutoff up to 00:00 Boston on the
    forecast day, as_of + 1 (module docstring)."""
    midnight = datetime.combine(as_of + timedelta(days=1), clock_time(0), BOSTON)
    return forecast_cutoff(as_of, root) <= run_after < midnight


@dataclass(frozen=True)
class DateRange:
    """The days a run covers, both ends inclusive (end None: through the latest data), the as-of date
    every step reads, and whether the run is an evening run (module docstring)."""

    start: date = FIRST_DAY
    end: date | None = None
    as_of: date | None = None
    evening: bool = False

    @classmethod
    def from_params(cls, params: Mapping, run_after: datetime | None = None) -> DateRange:
        """A range from the DAG params, checked: no unknown key, start on or after FIRST_DAY, end not before
        start, and at least one legacy year. as_of is the param when set, else the Boston date of run_after."""
        unknown = set(params) - set(PARAMS)
        if unknown:
            raise ValueError(f"unknown params {sorted(unknown)}; the DAG takes {', '.join(PARAMS)}")
        start = parse_day(params.get("start"), "start") or FIRST_DAY
        end = parse_day(params.get("end"), "end")
        as_of = parse_day(params.get("as_of"), "as_of") or (boston_date(run_after) if run_after else None)
        if start < FIRST_DAY:
            raise ValueError(f"start {start} is before {FIRST_DAY}, the first training day")
        if end is not None and end < start:
            raise ValueError(f"end {end} is before start {start}")
        if as_of is not None and as_of < FIRST_DAY:
            raise ValueError(f"as_of {as_of} is before {FIRST_DAY}, the first training day")
        days = cls(start, end, as_of)
        if not days.legacy_years:
            raise ValueError(
                f"no legacy 311 file in {days.label}; the files cover {LEGACY_YEARS[0]} to {LEGACY_YEARS[-1]}"
            )
        return days

    @property
    def is_full(self) -> bool:
        """The default range: the full historical build."""
        return self.start == FIRST_DAY and self.end is None

    @property
    def label(self) -> str:
        """The range as a folder name, start_end, with latest for an open end."""
        return f"{self.start}_{self.end or 'latest'}"

    @property
    def legacy_years(self) -> list[int]:
        """The legacy 311 file years the range touches."""
        last = self.end.year if self.end else LEGACY_YEARS[-1]
        return [year for year in LEGACY_YEARS if self.start.year <= year <= last]


@dataclass(frozen=True)
class RunPaths:
    """Where a run reads raw files and writes its outputs."""

    raw: Path
    interim: Path
    processed: Path


def run_paths(days: DateRange, root: Path = REPO) -> RunPaths:
    """The full build writes the standard folders. Any other range writes its own
    slice folder, so a test run never overwrites the files other scripts read."""
    data = root / "Data-Pipeline" / "data"
    base = data if days.is_full else data / "slices" / days.label
    return RunPaths(raw=data / "raw", interim=base / "interim", processed=base / "processed")


def legacy_year_path(interim: Path, year: int) -> Path:
    """Where parse_legacy_311 writes one year when the DAG parses the years side by side."""
    return interim / "legacy_311_by_year" / f"{year}.parquet"


@dataclass(frozen=True)
class StepFiles:
    """A real step's files. data_in and data_out are counted as rows in and out;
    config_in (config files and reference layers) is required but not counted."""

    data_in: list[Path] = field(default_factory=list)
    config_in: list[Path] = field(default_factory=list)
    data_out: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class StepPlan:
    """One real step in one place: the script's arguments, the files behind them,
    what to do when an input is missing, and a download folder whose empty files
    are fetched again. keep_outputs: the outputs are downloads the script replaces
    only when their source changed, so they are not deleted before it runs."""

    args: list
    files: StepFiles
    hint: str
    refetch: Path | None = None
    skip: str = ""  # why this run does not run the step; it logs the reason and succeeds
    env: tuple[str, ...] = ()  # credentials the script needs (#4), checked before it runs
    keep_outputs: bool = False
    # For the dvc.yaml stages (#18): folders the step adds to and keeps between runs
    # (downloads, the statistics history), and what else it writes beyond data_out
    # (folders, manifests, change points, check results), not counted as rows.
    keeps: tuple[Path, ...] = ()
    writes_too: tuple[Path, ...] = ()


def configured_layers(config: Path) -> list[str]:
    """Static layers that static_layers.py writes a parquet file for (vector and table)."""
    if not config.is_file():
        return []
    with config.open(newline="", encoding="utf-8-sig") as f:
        return [row["name"] for row in csv.DictReader(f) if row.get("kind") in ("vector", "table")]


def configured_311_files(config: Path, system: str) -> list[str]:
    """Files that ingest_311.py keeps in raw/311 for one system (legacy or new_system)."""
    if not config.is_file():
        return []
    with config.open(newline="", encoding="utf-8-sig") as f:
        return [row["filename"].strip() for row in csv.DictReader(f) if (row.get("system") or "").strip() == system]


def step_plan(step: str, days: DateRange, root: Path = REPO, year: int | None = None) -> StepPlan:
    """The table of real steps. Each path is named once and used for the arguments
    and the checks, so a step that #8, #16 or #32 adds is one more block. Every
    path is explicit, because the scripts' defaults are relative to the working
    directory. With a year, parse_legacy_311 parses only that year into its own
    file (#37)."""
    if step not in SCRIPTS and step not in LOCAL:
        raise ValueError(f"no real step {step!r}; real steps: {sorted([*SCRIPTS, *LOCAL])}")
    paths = run_paths(days, root)
    raw, interim = paths.raw, paths.interim
    config = root / "Data-Pipeline" / "config"
    as_of = ["--as-of", days.as_of] if days.as_of else []
    if step in ("ingest_legacy_311", "ingest_new_311"):
        # The downloads and their schedule state are the same for every range;
        # only the new-system parquet goes to the run's folder.
        system = "legacy" if step == "ingest_legacy_311" else "new_system"
        sources, raw_dir = config / "ingest_311_sources.csv", raw / "311"
        return StepPlan(
            args=["--raw-dir", raw_dir, "--sources", sources, "--state-dir", root / "Data-Pipeline" / "data" / "state" / "311",
                  "--prepared-dir", interim, "--system", system],
            files=StepFiles(config_in=[sources], data_out=[raw_dir / name for name in configured_311_files(sources, system)]),
            hint="Restore Data-Pipeline/config/ingest_311_sources.csv from git.",
            refetch=raw_dir,
            keep_outputs=True,
            # ingest_311.py writes new_system_as_published.parquet next to the run's outputs
            writes_too=(interim / "new_system_as_published.parquet",) if system == "new_system" else (),
        )
    if step == "schema_and_statistics":
        # #35. No data_out: the step compares with the previous run's files, so
        # they must not be cleared first, and it writes nothing when they are unchanged.
        located, counts = interim / "311_located.parquet", paths.processed / "daily_counts.parquet"
        thresholds = config / "stats_thresholds.csv"
        return StepPlan(
            args=["--located", located, "--daily-counts", counts,
                  "--thresholds", thresholds, "--out-dir", paths.processed / "stats"],
            files=StepFiles(data_in=[located], config_in=[thresholds]),
            hint="Run spatial_join and daily_counts for the same range first.",
            keeps=(paths.processed / "stats",),
        )
    if step == "ingest_static_layers":
        layers, raw_dir, out_dir = config / "static_layers.csv", raw / "static", interim / "static"
        return StepPlan(
            args=["--config", layers, "--raw-dir", raw_dir, "--out-dir", out_dir,
                  "--manifest", interim / "static-layers.csv", "--download"],
            files=StepFiles(config_in=[layers], data_out=[out_dir / f"{name}.parquet" for name in configured_layers(layers)]),
            hint="Restore Data-Pipeline/config/static_layers.csv from git.",
            refetch=raw_dir,
            keeps=(raw_dir,),
            writes_too=(out_dir, interim / "static-layers.csv"),
        )
    if step == "parse_legacy_311":
        raw_dir = raw / "311"
        years = days.legacy_years if year is None else [year]
        out = interim / "legacy_311.parquet" if year is None else legacy_year_path(interim, year)
        return StepPlan(
            args=["--raw-dir", raw_dir, "--years", *years, "--out", out],
            files=StepFiles(data_in=[raw_dir / f"{y}.csv" for y in years], data_out=[out]),
            hint="Put the yearly legacy 311 files in Data-Pipeline/data/raw/311/ (ingest_legacy_311 downloads them).",
        )
    if step == "merge_legacy_311":
        return StepPlan(
            args=[],  # merge_legacy_years does it here, not a script
            files=StepFiles(
                data_in=[legacy_year_path(interim, y) for y in days.legacy_years],
                data_out=[interim / "legacy_311.parquet"],
            ),
            hint="Run parse_legacy_311 for every year in the range first.",
        )
    if step == "parse_new_311":
        export, out = raw / "311" / "new_system.csv", interim / "new_311.parquet"
        return StepPlan(
            args=["--raw", export, "--out", out],
            files=StepFiles(data_in=[export], data_out=[out]),
            hint="Put the new-system export in Data-Pipeline/data/raw/311/ (ingest_new_311 downloads it).",
        )
    if step == "combine_311":
        legacy, new, out = interim / "legacy_311.parquet", interim / "new_311.parquet", interim / "311_combined.parquet"
        category_map = root / "docs" / "data" / "category-map.csv"
        return StepPlan(
            args=["--legacy", legacy, "--new", new, "--map", category_map,
                  "--out", out, "--change-points", interim / "change-points.csv"],
            files=StepFiles(data_in=[legacy, new], config_in=[category_map], data_out=[out]),
            hint="Run parse_legacy_311 and parse_new_311 for the same range first.",
            writes_too=(interim / "change-points.csv",),
        )
    if step == "daily_counts":
        located, out = interim / "311_located.parquet", paths.processed / "daily_counts.parquet"
        hoods, processing = interim / "static" / "neighborhoods_by_tract20.parquet", config / "processing.csv"
        switches = interim / "change-points.csv"  # combine_311's switch dates set where the counts end
        end = ["--end", days.end] if days.end else []
        return StepPlan(
            args=["--located", located, "--neighborhoods", hoods, "--config", processing,
                  "--change-points", switches, "--start", days.start, *end, "--out", out],
            files=StepFiles(data_in=[located], config_in=[hoods, processing, switches], data_out=[out]),
            hint="Run spatial_join for the same range first.",
        )
    if step == "ingest_alert_history":
        # #16, ADR-014: one IEM bulk pull per year for office BOX into the shared raw folder; the
        # prepared alerts cover every year whatever the range, since the features need the history.
        # When each year was pulled goes to data/state/, like the #7 and #8 manifests.
        # An evening run (#160) also checks the pull against ingest_nws_alerts' snapshot, taken after the
        # cutoff, and fails before writing when a message the NWS sent before the cutoff is missing.
        raw_dir, alert_map = raw / "nws_alerts", root / "docs" / "data" / "alert-map.csv"
        out, settings = interim / "nws_alerts.parquet", config / "labels_features.csv"
        fresh = ["--check-nws", raw / "weather" / "nws_alerts", "--settings", settings] if days.evening else []
        return StepPlan(
            args=["--raw-dir", raw_dir, "--state-dir", raw.parent / "state" / "nws_alerts",
                  "--map", alert_map, "--out", out, "--download", *as_of, *fresh],
            files=StepFiles(config_in=[alert_map, *([settings] if days.evening else [])], data_out=[out]),
            hint="Restore docs/data/alert-map.csv from git" + (" and run ingest_nws_alerts first." if days.evening else "."),
            refetch=raw_dir,
            keeps=(raw_dir,),
        )
    if step in ("surge_labels", "feature_table"):
        # #16. A label needs the same weeks of the 3 years before its day (ADR-005) and a feature the
        # 28 days before, so both read the full build's daily counts; a slice writes only its days.
        counts = run_paths(DateRange(), root).processed / "daily_counts.parquet"
        settings, end = config / "labels_features.csv", (["--end", days.end] if days.end else [])
        if step == "surge_labels":
            rules, out = config / "surge_rules.csv", paths.processed / "labels.parquet"
            return StepPlan(
                args=["--counts", counts, "--rules", rules, "--settings", settings,
                      "--start", days.start, *end, "--out", out],
                files=StepFiles(data_in=[counts], config_in=[rules, settings], data_out=[out]),
                hint="Run daily_counts for the full build (start 2015-01-01, no end) first.",
            )
        # Weather and tides (#7, #8) come from the shared raw folders the ingest tasks fill; the
        # folders are required, so a missing download stops the task before it runs.
        alerts, static, out = interim / "nws_alerts.parquet", interim / "static", paths.processed / "features.parquet"
        noaa, forecasts = raw / "weather" / "noaa_logan", raw / "open_meteo" / "day_before_ncep_gfs_global"
        tides = raw / "tides" / "predicted"
        return StepPlan(
            args=["--counts", counts, "--alerts", alerts, "--static-dir", static, "--settings", settings,
                  "--noaa-dir", noaa, "--forecast-dir", forecasts, "--tides-dir", tides,
                  "--start", days.start, *end, *as_of, "--out", out],
            files=StepFiles(data_in=[counts, alerts], config_in=[static, settings, noaa, forecasts, tides], data_out=[out]),
            hint="Run daily_counts for the full build, ingest_alert_history, ingest_static_layers, "
                 "ingest_noaa_logan, ingest_tides and ingest_open_meteo first.",
        )
    if step == "bias_slicing":
        # #32. Reads the labels and the located requests of the same range; writes the rates, the
        # tests and the report, nothing the later steps read.
        labels, located, static = paths.processed / "labels.parquet", interim / "311_located.parquet", interim / "static"
        settings, out = config / "bias_slicing.csv", interim / "reports" / "bias"
        return StepPlan(
            args=["--labels", labels, "--located", located, "--static-dir", static,
                  "--config", settings, "--out-dir", out],
            files=StepFiles(data_in=[labels, located], config_in=[static, settings],
                            data_out=[out / name for name in ("slices.csv", "gaps.csv", "report.md")]),
            hint="Run surge_labels, spatial_join and ingest_static_layers for the same range first.",
        )
    if step in ("ingest_noaa_logan", "ingest_nws_forecasts", "ingest_nws_alerts", "ingest_tides"):
        # #7. Raw downloads add to their folders and keep history, so they have no
        # data_out to clear; a missing key fails before the script runs. Their
        # manifests live in data/state/, shared by every date range like raw/.
        weather, state = raw / "weather", raw.parent / "state"
        folder = {"ingest_noaa_logan": weather / "noaa_logan", "ingest_nws_forecasts": weather / "nws_forecasts",
                  "ingest_nws_alerts": weather / "nws_alerts", "ingest_tides": raw / "tides"}[step]
        args, env = {
            "ingest_noaa_logan": (["--raw-dir", folder, "--state-dir", state / "weather" / "noaa_logan", *as_of],
                                  ("NOAA_CDO_TOKEN",)),
            "ingest_nws_forecasts": (["forecasts", "--raw-dir", folder], ("NWS_USER_AGENT",)),
            "ingest_nws_alerts": (["alerts", "--raw-dir", folder], ("NWS_USER_AGENT",)),
            "ingest_tides": (["--raw-dir", folder, "--state-dir", state / "tides", *as_of], ()),
        }[step]
        return StepPlan(args=args, files=StepFiles(), hint='Team members: fill .env with the runbook command ("API keys in .env").',
                        env=env, keeps=(folder,))
    if step == "ingest_open_meteo":
        # #8: every set in config/open_meteo.csv. The year files add to the shared raw folder and keep
        # their history, so there is no data_out to clear; no key is needed. Manifests, run logs and the
        # attribution rows go to data/state/, since docs/data is read-only in the containers (the
        # committed docs/data/open-meteo.csv comes from a run of the script, runbook #8 section).
        sets, state = config / "open_meteo.csv", raw.parent / "state" / "open_meteo"
        return StepPlan(
            args=["--config", sets, "--raw-dir", raw / "open_meteo", "--state-dir", state,
                  "--attribution", state / "open-meteo.csv", *as_of, *(["--evening"] if days.evening else [])],
            files=StepFiles(config_in=[sets]),
            hint="Restore Data-Pipeline/config/open_meteo.csv from git.",
            keeps=(raw / "open_meteo",),
        )
    if step == "validate_raw":
        # #17. Only rows in are counted: the step writes a JSON of check results, not data.
        raw_dir, bounds, published = raw / "311", config / "validation_bounds.csv", config / "published_311_counts.csv"
        report = interim / "validation" / "raw.json"
        return StepPlan(
            args=["--stage", "raw", "--config", bounds, "--raw-dir", raw_dir, "--years", *days.legacy_years,
                  "--published", published, "--out", report],
            files=StepFiles(
                data_in=[*(raw_dir / f"{year}.csv" for year in days.legacy_years), raw_dir / "new_system.csv"],
                config_in=[bounds, published],
            ),
            hint="Put the legacy 311 files and the new-system export in Data-Pipeline/data/raw/311/ (ingest_legacy_311 and ingest_new_311 download them).",
            writes_too=(report,),
        )
    if step == "validate_processed":
        combined, located = interim / "311_combined.parquet", interim / "311_located.parquet"
        bounds, hoods = config / "validation_bounds.csv", interim / "static" / "neighborhoods_by_tract20.parquet"
        report = interim / "validation" / "processed.json"
        return StepPlan(
            args=["--stage", "processed", "--config", bounds, "--combined", combined, "--located", located,
                  "--neighborhoods", hoods, *(["--latest", (days.end or days.as_of).isoformat()] if days.end or days.as_of else []),
                  "--out", report],
            files=StepFiles(data_in=[combined, located], config_in=[bounds, hoods]),
            hint="Run spatial_join and ingest_static_layers for the same range first.",
            writes_too=(report,),
        )
    if step == "forecast_rows":
        # #160: tomorrow's rows, from the full build's counts and the evening's downloads, to
        # data/forecast/<as_of>/ for the scoring step (D8). Only an evening run of the full build writes them.
        full = run_paths(DateRange(), root)
        counts, alerts, static = full.processed / "daily_counts.parquet", full.interim / "nws_alerts.parquet", full.interim / "static"
        settings, gfs = config / "labels_features.csv", raw / "open_meteo" / "day_before_ncep_gfs_global"
        catalog = config / "feature_catalog.csv"  # its use_in_model = yes features are the /predict inputs
        noaa, tides = raw / "weather" / "noaa_logan", raw / "tides" / "predicted"
        gfs_state = raw.parent / "state" / "open_meteo" / "day_before_ncep_gfs_global"
        out = forecast_path(days.as_of, root) if days.as_of else root / "Data-Pipeline" / "data" / "forecast"
        skip = ("" if days.evening and days.is_full else
                f"{days.label} is not an evening run of the full build, so tomorrow's rows are not written")
        return StepPlan(
            args=["--counts", counts, "--alerts", alerts, "--static-dir", static, "--settings", settings,
                  "--noaa-dir", noaa, "--forecast-dir", gfs, "--forecast-state", gfs_state, "--tides-dir", tides,
                  "--catalog", catalog, *as_of, "--out", out],
            files=StepFiles(data_in=[counts, alerts], config_in=[static, settings, catalog, noaa, gfs, gfs_state, tides],
                            data_out=[out]),
            hint="Run the full build's daily_counts, ingest_alert_history, ingest_open_meteo and ingest_tides "
                 "for the same as-of date first.",
            skip=skip,
        )
    # spatial_join (#14)
    combined, out = interim / "311_combined.parquet", interim / "311_located.parquet"
    tracts, hoods = interim / "static" / "tracts_2020.parquet", interim / "static" / "neighborhoods_by_tract20.parquet"
    placeholders = config / "placeholder_locations.csv"
    return StepPlan(
        args=["--combined", combined, "--tracts", tracts, "--neighborhoods", hoods,
              "--placeholders", placeholders, "--out", out],
        files=StepFiles(data_in=[combined], config_in=[tracts, hoods, placeholders], data_out=[out]),
        hint="Run combine_311 and ingest_static_layers for the same range first.",
    )


def forecast_path(as_of: date, root: Path = REPO) -> Path:
    """Where forecast_rows writes the rows for the day after as_of."""
    return root / "Data-Pipeline" / "data" / "forecast" / as_of.isoformat() / "features.parquet"


def script_args(step: str, days: DateRange, root: Path = REPO, year: int | None = None) -> list[str]:
    """Command-line arguments of a real step, from step_plan."""
    return [str(arg) for arg in step_plan(step, days, root, year).args]


def step_files(step: str, days: DateRange, root: Path = REPO, year: int | None = None) -> StepFiles:
    """The files behind script_args, from step_plan."""
    return step_plan(step, days, root, year).files


def check_inputs(step: str, plan: StepPlan) -> None:
    """Stop before the script runs when an input is missing or empty (an empty download)."""
    needed = plan.files.data_in + plan.files.config_in
    missing = [path for path in needed if not path.exists()]
    empty = [path for path in needed if path.is_file() and path.stat().st_size == 0]
    unset = [name for name in plan.env if not os.environ.get(name, "").strip()]
    problems = []
    if unset:
        problems.append(f"{', '.join(unset)} not set (add to .env, names in .env.example; docs/data-access.md)")
    if missing:
        problems.append("missing " + ", ".join(str(path) for path in missing))
    if empty:
        problems.append("empty " + ", ".join(str(path) for path in empty))
    if problems:
        raise InputError(f"{step} cannot start: {'; '.join(problems)}. {plan.hint}")


def clear_outputs(plan: StepPlan) -> None:
    """Delete the step's outputs, so a file left by an earlier run cannot pass for new output.
    Downloads (keep_outputs) stay: deleting them would force every file to be fetched again."""
    if plan.keep_outputs:
        return
    for path in plan.files.data_out:
        path.unlink(missing_ok=True)


def drop_empty_downloads(plan: StepPlan) -> list[Path]:
    """Delete 0-byte files in the step's download folder, so the download runs again."""
    if plan.refetch is None or not plan.refetch.is_dir():
        return []
    empty = sorted(path for path in plan.refetch.iterdir() if path.is_file() and path.stat().st_size == 0)
    for path in empty:
        path.unlink()
    return empty


def check_outputs(step: str, plan: StepPlan) -> None:
    """Fail when a step finished without writing one of its outputs."""
    missing = [path for path in plan.files.data_out if not path.exists()]
    if missing:
        raise RuntimeError(f"{step} finished but wrote no {', '.join(str(path) for path in missing)}")


def count_rows(path: Path) -> int:
    """Rows in a data file: a parquet file's row count from its metadata, or a CSV's
    records counted the way the parsers report file_rows (count_csv_records)."""
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq  # here, so importing this module stays light for the DAG

        return pq.ParquetFile(path).metadata.num_rows
    return count_csv_records(path)


def count_csv_records(path: Path) -> int:
    """A CSV's records as legacy_311.count_records counts them: no header, no empty
    lines, and a line break inside a quoted field keeps one record.

    A quoted field can only span lines when a line holds an odd number of quote
    marks, so while every line is even the records are the non-empty lines. At
    the first odd line it hands over to legacy_311.count_records. The 16 MB
    buffer matters in the containers: small reads through the Windows bind
    mount are what make a CSV reader slow there.
    """
    lines = 0
    with path.open("rb", buffering=1 << 24) as f:
        for line in f:
            if line.count(b'"') % 2:
                from legacy_311 import count_records  # here, since legacy_311 imports pandas

                return count_records(path)
            if line.rstrip(b"\r\n"):
                lines += 1
    return max(lines - 1, 0)


def log_task(run_id: str, task: str, rows_in: int | None, rows_out: int | None, seconds: float | None = None) -> None:
    """The line every task logs (#36): run id, rows in and rows out; n/a when a task has none."""
    def show(rows):
        return "n/a" if rows is None else f"{rows:,}"

    took = "" if seconds is None else f" in {seconds:.1f} s"
    log.info("run %s task %s: rows in %s, rows out %s%s", run_id, task, show(rows_in), show(rows_out), took)


def run_script(module: str, args: list[str]) -> None:
    """Run <module>.main() as `python <module>.py <args>` would.

    A clean exit counts as success. Any other exit code (argparse uses 2 for
    bad arguments) becomes a RuntimeError that names the script.
    """
    main = importlib.import_module(module).main
    saved = sys.argv
    sys.argv = [f"{module}.py", *args]
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            raise RuntimeError(f"{module}.py exited with code {e.code}") from e
    finally:
        sys.argv = saved
        sys.stdout.flush()


def trim_to_range(path: Path, column: str, days: DateRange) -> tuple[int, int]:
    """Keep the rows of a parquet file whose time falls in the range, both ends inclusive.

    Times are Boston local wall-clock times. A row without a time is dropped;
    there are none, since the parsers already drop rows without an open time.
    Returns (rows before, rows kept).
    """
    import pandas as pd  # here, so importing this module stays light for the DAG

    df = pd.read_parquet(path)
    keep = df[column] >= pd.Timestamp(days.start)
    if days.end is not None:
        keep &= df[column] < pd.Timestamp(days.end) + pd.Timedelta(days=1)
    df[keep].reset_index(drop=True).to_parquet(path, index=False)
    return len(df), int(keep.sum())


def merge_legacy_years(files: StepFiles) -> None:
    """Join the per-year legacy files (data_in, in year order) into legacy_311.parquet
    the way legacy_311.read_years joins the years: dropping a case id already seen
    in an earlier year. So the result is the same table as one parse of all the
    years."""
    import pandas as pd  # here, so importing this module stays light for the DAG

    years = pd.concat([pd.read_parquet(path) for path in files.data_in], ignore_index=True)
    repeated = years["case_enquiry_id"].duplicated(keep="first")
    years[~repeated].reset_index(drop=True).to_parquet(files.data_out[0], index=False)
    log.info("merged %s years: dropped %s case ids repeated across years", len(files.data_in), f"{int(repeated.sum()):,}")


# Steps done here rather than by a pipeline script.
LOCAL = {"merge_legacy_311": merge_legacy_years}


def run_step(
    step: str, days: DateRange, root: Path = REPO, run_id: str = "manual", year: int | None = None
) -> tuple[int | None, int | None]:
    """Run one real step and log its run id, rows in and rows out. Returns (rows in, rows out).

    With a year, parse_legacy_311 parses only that year (the DAG maps it over the
    range's years) and its file is not cut: merge_legacy_311 cuts the merged table.
    """
    task = step if year is None else f"{step}[{year}]"
    plan = step_plan(step, days, root, year)
    if plan.skip:
        log.info("run %s task %s: skipped: %s", run_id, task, plan.skip)
        log_task(run_id, task, None, None)
        return None, None
    check_inputs(step, plan)
    started = time.monotonic()
    try:
        for path in drop_empty_downloads(plan):
            log.warning("run %s task %s: deleted the empty download %s so it is fetched again", run_id, task, path)
        clear_outputs(plan)
        if step in LOCAL:
            LOCAL[step](plan.files)
        else:
            run_script(SCRIPTS[step], [str(arg) for arg in plan.args])
        check_outputs(step, plan)
        if not days.is_full and year is None:
            interim = run_paths(days, root).interim
            for name, column in TRIM.get(step, []):
                before, kept = trim_to_range(interim / name, column, days)
                log.info("run %s task %s: kept %s of %s rows of %s in %s", run_id, task, f"{kept:,}", f"{before:,}", name, days.label)
        rows_in = sum(count_rows(path) for path in plan.files.data_in) if plan.files.data_in else None
        rows_out = sum(count_rows(path) for path in plan.files.data_out) if plan.files.data_out else None
    except Exception as e:
        log.error("run %s task %s failed: %s: %s", run_id, task, type(e).__name__, e)
        raise
    log_task(run_id, task, rows_in, rows_out, time.monotonic() - started)
    return rows_in, rows_out


def main(argv: list[str] | None = None) -> None:
    """Run one real step of the full build, as `python scripts/pipeline_steps.py <step>` in a dvc.yaml
    stage (#18): the run_step the DAG task calls, with run id dvc. A missing input or key exits with
    its message, as InputError fails the DAG task."""
    parser = argparse.ArgumentParser(description="Run one daily_pipeline step for the full build (the dvc.yaml stages).")
    parser.add_argument("step", choices=sorted([*SCRIPTS, *LOCAL]))
    parser.add_argument("--year", type=int, help="parse_legacy_311 only: parse this legacy year into its own file")
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="the as-of date, YYYY-MM-DD in Boston; default: BRT_AS_OF, and one of the two is required")
    args = parser.parse_args(argv)
    if args.year is not None and (args.step != "parse_legacy_311" or args.year not in LEGACY_YEARS):
        parser.error(f"--year is for parse_legacy_311 only, one of {LEGACY_YEARS[0]} to {LEGACY_YEARS[-1]}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # BRT_AS_OF holds one date across the stages of a dvc repro, which each start their own process; a stage
    # never falls back to the clock, since a repro that crosses midnight would give its stages two dates.
    as_of = args.as_of or parse_day(os.environ.get("BRT_AS_OF"), "BRT_AS_OF")
    if as_of is None:
        parser.error("set --as-of or BRT_AS_OF (YYYY-MM-DD, Boston): every stage of a dvc repro reads one as-of date")
    try:
        run_step(args.step, DateRange(as_of=as_of), year=args.year, run_id="dvc")
    except InputError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
