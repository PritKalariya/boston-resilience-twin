"""Steps of the daily_pipeline DAG (#22): date range, run folders and script arguments.

The DAG (Data-Pipeline/dags/daily_pipeline.py) imports this module through
PYTHONPATH (docker-compose.yaml, ADR-017), and the tests import it through
conftest.py. It uses the standard library only, so Airflow parses the DAG
without loading pandas or geopandas.

A real step calls a pipeline script's own main() with the arguments it would
get on the command line (run_script), so the scripts stay unchanged and the
DAG and the dvc.yaml stages (#18) share one entry point per script. Each real
step is one block in step_plan: its arguments, the files behind them and what
to do when an input is missing. Every other task is a placeholder until the
issue in PLANNED builds it.

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
before its script runs, so a file left by an earlier run cannot pass for new
output, and empty files in a download folder are deleted so the download runs
again. A script error is logged with the run and the task before it is raised
again unchanged. Tasks in NETWORK retry; the others fail at once, and their
failure alert comes from alerts.py.
"""

from __future__ import annotations

import csv
import importlib
import logging
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # /opt/airflow/repo in the Airflow containers
FIRST_DAY = date(2015, 1, 1)  # first day of the training years (train 2015 to 2023)
LEGACY_YEARS = range(2015, 2027)  # yearly legacy 311 files, as in legacy_311.py
PARAMS = ("start", "end")

log = logging.getLogger(__name__)

# Task id -> pipeline script in Data-Pipeline/scripts.
SCRIPTS = {
    "ingest_static_layers": "static_layers",
    "parse_legacy_311": "legacy_311",
    "parse_new_311": "new_311",
    "combine_311": "map_311",
    "spatial_join": "spatial_join",
    "validate_raw": "validate_data",
    "validate_processed": "validate_data",
}
# Task id -> the issue that builds it; a placeholder in the DAG until then.
PLANNED = {
    "ingest_legacy_311": "#6",
    "ingest_new_311": "#6",
    "ingest_noaa_logan": "#7",
    "ingest_nws_forecasts": "#7",
    "ingest_nws_alerts": "#7",
    "ingest_tides": "#7",
    "ingest_open_meteo": "#8",
    "daily_counts": "#15",
    "labels_and_features": "#16",
    "schema_and_statistics": "#35",
    "bias_slicing": "#32",
}
# Task id -> the outputs a slice cuts to its days afterwards, with their time column.
TRIM = {
    "parse_legacy_311": [("legacy_311.parquet", "open_dt")],
    "combine_311": [("311_combined.parquet", "open_dt")],
}

# Tasks that fetch from the network retry twice. Airflow's exponential backoff
# adds a random spread, so the waits are about 5 to 10 minutes and then 10 to 15
# (capped). The placeholders are listed too, so #6, #7 and #8 get the same
# retries when they fill them.
NETWORK = {task for task in (*SCRIPTS, *PLANNED) if task.startswith("ingest_")}
RETRIES = 2
RETRY_DELAY = timedelta(minutes=5)
RETRY_BACKOFF = 2.0  # each retry's base wait is twice the one before
MAX_RETRY_DELAY = timedelta(minutes=15)


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


@dataclass(frozen=True)
class DateRange:
    """The days a run covers, both ends inclusive. end None: through the latest data."""

    start: date = FIRST_DAY
    end: date | None = None

    @classmethod
    def from_params(cls, params: Mapping) -> DateRange:
        unknown = set(params) - set(PARAMS)
        if unknown:
            raise ValueError(f"unknown params {sorted(unknown)}; the DAG takes {' and '.join(PARAMS)}")
        start = parse_day(params.get("start"), "start") or FIRST_DAY
        end = parse_day(params.get("end"), "end")
        if start < FIRST_DAY:
            raise ValueError(f"start {start} is before {FIRST_DAY}, the first training day")
        if end is not None and end < start:
            raise ValueError(f"end {end} is before start {start}")
        days = cls(start, end)
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
        return f"{self.start}_{self.end or 'latest'}"

    @property
    def legacy_years(self) -> list[int]:
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
    are fetched again."""

    args: list
    files: StepFiles
    hint: str
    refetch: Path | None = None


def configured_layers(config: Path) -> list[str]:
    """Static layers that static_layers.py writes a parquet file for (vector and table)."""
    if not config.is_file():
        return []
    with config.open(newline="", encoding="utf-8-sig") as f:
        return [row["name"] for row in csv.DictReader(f) if row.get("kind") in ("vector", "table")]


def step_plan(step: str, days: DateRange, root: Path = REPO) -> StepPlan:
    """The table of real steps. Each path is named once and used for the arguments
    and the checks, so a step that #6, #7, #8 or #15 adds is one more block. Every
    path is explicit, because the scripts' defaults are relative to the working
    directory."""
    if step not in SCRIPTS:
        raise ValueError(f"no script step {step!r}; real steps: {sorted(SCRIPTS)}")
    paths = run_paths(days, root)
    raw, interim = paths.raw, paths.interim
    config = root / "Data-Pipeline" / "config"
    hint_311 = "(by hand until #6 lands)"
    if step == "ingest_static_layers":
        layers, raw_dir, out_dir = config / "static_layers.csv", raw / "static", interim / "static"
        return StepPlan(
            args=["--config", layers, "--raw-dir", raw_dir, "--out-dir", out_dir,
                  "--manifest", interim / "static-layers.csv", "--download"],
            files=StepFiles(config_in=[layers], data_out=[out_dir / f"{name}.parquet" for name in configured_layers(layers)]),
            hint="Restore Data-Pipeline/config/static_layers.csv from git.",
            refetch=raw_dir,
        )
    if step == "parse_legacy_311":
        raw_dir, out = raw / "311", interim / "legacy_311.parquet"
        return StepPlan(
            args=["--raw-dir", raw_dir, "--years", *days.legacy_years, "--out", out],
            files=StepFiles(data_in=[raw_dir / f"{year}.csv" for year in days.legacy_years], data_out=[out]),
            hint=f"Put the yearly legacy 311 files in Data-Pipeline/data/raw/311/ {hint_311}.",
        )
    if step == "parse_new_311":
        export, out = raw / "311" / "new_system.csv", interim / "new_311.parquet"
        return StepPlan(
            args=["--raw", export, "--out", out],
            files=StepFiles(data_in=[export], data_out=[out]),
            hint=f"Put the new-system export in Data-Pipeline/data/raw/311/ {hint_311}.",
        )
    if step == "combine_311":
        legacy, new, out = interim / "legacy_311.parquet", interim / "new_311.parquet", interim / "311_combined.parquet"
        category_map = root / "docs" / "data" / "category-map.csv"
        return StepPlan(
            args=["--legacy", legacy, "--new", new, "--map", category_map,
                  "--out", out, "--change-points", interim / "change-points.csv"],
            files=StepFiles(data_in=[legacy, new], config_in=[category_map], data_out=[out]),
            hint="Run parse_legacy_311 and parse_new_311 for the same range first.",
        )
    if step == "validate_raw":
        # #17. Only rows in are counted: the step writes a JSON of check results, not data.
        raw_dir, bounds, published = raw / "311", config / "validation_bounds.csv", config / "published_311_counts.csv"
        return StepPlan(
            args=["--stage", "raw", "--config", bounds, "--raw-dir", raw_dir, "--years", *days.legacy_years,
                  "--published", published, "--out", interim / "validation" / "raw.json"],
            files=StepFiles(
                data_in=[*(raw_dir / f"{year}.csv" for year in days.legacy_years), raw_dir / "new_system.csv"],
                config_in=[bounds, published],
            ),
            hint=f"Put the legacy 311 files and the new-system export in Data-Pipeline/data/raw/311/ {hint_311}.",
        )
    if step == "validate_processed":
        combined, located = interim / "311_combined.parquet", interim / "311_located.parquet"
        bounds, hoods = config / "validation_bounds.csv", interim / "static" / "neighborhoods_by_tract20.parquet"
        return StepPlan(
            args=["--stage", "processed", "--config", bounds, "--combined", combined, "--located", located,
                  "--neighborhoods", hoods, *(["--latest", days.end.isoformat()] if days.end else []),
                  "--out", interim / "validation" / "processed.json"],
            files=StepFiles(data_in=[combined, located], config_in=[bounds, hoods]),
            hint="Run spatial_join and ingest_static_layers for the same range first.",
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


def script_args(step: str, days: DateRange, root: Path = REPO) -> list[str]:
    """Command-line arguments of a real step, from step_plan."""
    return [str(arg) for arg in step_plan(step, days, root).args]


def step_files(step: str, days: DateRange, root: Path = REPO) -> StepFiles:
    """The files behind script_args, from step_plan."""
    return step_plan(step, days, root).files


def check_inputs(step: str, plan: StepPlan) -> None:
    """Stop before the script runs when an input is missing or empty (an empty download)."""
    needed = plan.files.data_in + plan.files.config_in
    missing = [path for path in needed if not path.exists()]
    empty = [path for path in needed if path.is_file() and path.stat().st_size == 0]
    problems = []
    if missing:
        problems.append("missing " + ", ".join(str(path) for path in missing))
    if empty:
        problems.append("empty " + ", ".join(str(path) for path in empty))
    if problems:
        raise InputError(f"{step} cannot start: {'; '.join(problems)}. {plan.hint}")


def clear_outputs(plan: StepPlan) -> None:
    """Delete the step's outputs, so a file left by an earlier run cannot pass for new output."""
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


def run_step(step: str, days: DateRange, root: Path = REPO, run_id: str = "manual") -> tuple[int | None, int]:
    """Run one real step and log its run id, rows in and rows out. Returns (rows in, rows out)."""
    plan = step_plan(step, days, root)
    check_inputs(step, plan)
    started = time.monotonic()
    try:
        for path in drop_empty_downloads(plan):
            log.warning("run %s task %s: deleted the empty download %s so it is fetched again", run_id, step, path)
        clear_outputs(plan)
        run_script(SCRIPTS[step], [str(arg) for arg in plan.args])
        check_outputs(step, plan)
        if not days.is_full:
            interim = run_paths(days, root).interim
            for name, column in TRIM.get(step, []):
                before, kept = trim_to_range(interim / name, column, days)
                log.info("run %s task %s: kept %s of %s rows of %s in %s", run_id, step, f"{kept:,}", f"{before:,}", name, days.label)
        rows_in = sum(count_rows(path) for path in plan.files.data_in) if plan.files.data_in else None
        rows_out = sum(count_rows(path) for path in plan.files.data_out)
    except Exception as e:
        log.error("run %s task %s failed: %s: %s", run_id, step, type(e).__name__, e)
        raise
    log_task(run_id, step, rows_in, rows_out, time.monotonic() - started)
    return rows_in, rows_out
