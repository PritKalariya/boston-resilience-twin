"""Steps of the daily_pipeline DAG (#22): date range, run folders and script arguments.

The DAG (Data-Pipeline/dags/daily_pipeline.py) imports this module through
PYTHONPATH (docker-compose.yaml, ADR-017), and the tests import it through
conftest.py. It uses the standard library only, so Airflow parses the DAG
without loading pandas or geopandas.

A real step calls a pipeline script's own main() with the arguments it would
get on the command line (run_script), so the scripts stay unchanged and the
DAG and the dvc.yaml stages (#18) share one entry point per script. Every
other task is a placeholder until the issue in PLANNED builds it.

A slice keeps only its days, in Boston local time (new-system times are
already local, ADR-020): legacy rows right after parsing, and the combined
table after combining (TRIM). The new-system export stays whole, because
map_311 needs every new case to set the switch dates and to drop legacy
copies resubmitted to the new system. So a slice's combined table holds the
same rows as the full build has for those days.

Logging and failures (#36): every task logs one line with its run id, rows
in and rows out (log_task). A real step fails before its script runs when an
input file is missing or empty (InputError), and a script error is logged with
the run and the task before it is raised again unchanged. Tasks in NETWORK
retry; the others fail at once, and their failure alert comes from alerts.py.
"""

from __future__ import annotations

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
    "validate_raw": "#17",
    "spatial_join": "#14",
    "daily_counts": "#15",
    "labels_and_features": "#16",
    "validate_processed": "#17",
    "schema_and_statistics": "#35",
    "bias_slicing": "#32",
}
# Task id -> the outputs a slice cuts to its days afterwards, with their time column.
TRIM = {
    "parse_legacy_311": [("legacy_311.parquet", "open_dt")],
    "combine_311": [("311_combined.parquet", "open_dt")],
}

# Tasks that fetch from the network retry: 5 minutes, then 10. The placeholders
# are listed too, so #6, #7 and #8 get the same retries when they fill them.
NETWORK = {task for task in (*SCRIPTS, *PLANNED) if task.startswith("ingest_")}
RETRIES = 2
RETRY_DELAY = timedelta(minutes=5)
RETRY_BACKOFF = 2.0  # each retry waits twice as long as the one before
MAX_RETRY_DELAY = timedelta(minutes=15)

# What to do when a real step's input is missing or empty.
HINTS = {
    "ingest_static_layers": "Restore Data-Pipeline/config/static_layers.csv from git.",
    "parse_legacy_311": "Put the yearly legacy 311 files in Data-Pipeline/data/raw/311/ (by hand until #6 lands).",
    "parse_new_311": "Put the new-system export in Data-Pipeline/data/raw/311/ (by hand until #6 lands).",
    "combine_311": "Run parse_legacy_311 and parse_new_311 for the same range first.",
}


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


def script_args(step: str, days: DateRange, root: Path = REPO) -> list[str]:
    """Command-line arguments of a real step. Every path is explicit, because the
    scripts' defaults are relative to the working directory."""
    if step not in SCRIPTS:
        raise ValueError(f"no script step {step!r}; real steps: {sorted(SCRIPTS)}")
    paths = run_paths(days, root)
    raw, interim = paths.raw, paths.interim
    args = {
        "ingest_static_layers": [
            "--config", root / "Data-Pipeline" / "config" / "static_layers.csv",
            "--raw-dir", raw / "static",
            "--out-dir", interim / "static",
            "--manifest", interim / "static-layers.csv",
            "--download",
        ],
        "parse_legacy_311": [
            "--raw-dir", raw / "311",
            "--years", *days.legacy_years,
            "--out", interim / "legacy_311.parquet",
        ],
        "parse_new_311": [
            "--raw", raw / "311" / "new_system.csv",
            "--out", interim / "new_311.parquet",
        ],
        "combine_311": [
            "--legacy", interim / "legacy_311.parquet",
            "--new", interim / "new_311.parquet",
            "--map", root / "docs" / "data" / "category-map.csv",
            "--out", interim / "311_combined.parquet",
            "--change-points", interim / "change-points.csv",
        ],
    }[step]
    return [str(arg) for arg in args]


@dataclass(frozen=True)
class StepFiles:
    """A real step's files: data in and out are counted as rows in and out;
    config in is required but not counted."""

    data_in: list[Path] = field(default_factory=list)
    config_in: list[Path] = field(default_factory=list)
    data_out: list[Path] = field(default_factory=list)


def step_files(step: str, days: DateRange, root: Path = REPO) -> StepFiles:
    """The files behind script_args. The static layers' raw files are not inputs:
    the step downloads the ones that are missing."""
    paths = run_paths(days, root)
    raw, interim = paths.raw, paths.interim
    return {
        "ingest_static_layers": StepFiles(
            config_in=[root / "Data-Pipeline" / "config" / "static_layers.csv"],
            data_out=[interim / "static"],
        ),
        "parse_legacy_311": StepFiles(
            data_in=[raw / "311" / f"{year}.csv" for year in days.legacy_years],
            data_out=[interim / "legacy_311.parquet"],
        ),
        "parse_new_311": StepFiles(
            data_in=[raw / "311" / "new_system.csv"],
            data_out=[interim / "new_311.parquet"],
        ),
        "combine_311": StepFiles(
            data_in=[interim / "legacy_311.parquet", interim / "new_311.parquet"],
            config_in=[root / "docs" / "data" / "category-map.csv"],
            data_out=[interim / "311_combined.parquet"],
        ),
    }[step]


def check_inputs(step: str, files: StepFiles) -> None:
    """Stop before the script runs when an input is missing or empty (an empty download)."""
    needed = files.data_in + files.config_in
    missing = [path for path in needed if not path.exists()]
    empty = [path for path in needed if path.is_file() and path.stat().st_size == 0]
    problems = []
    if missing:
        problems.append("missing " + ", ".join(str(path) for path in missing))
    if empty:
        problems.append("empty " + ", ".join(str(path) for path in empty))
    if problems:
        raise InputError(f"{step} cannot start: {'; '.join(problems)}. {HINTS[step]}")


def check_outputs(step: str, files: StepFiles) -> None:
    missing = [path for path in files.data_out if not path.exists()]
    if missing:
        raise RuntimeError(f"{step} finished but wrote no {', '.join(str(path) for path in missing)}")


def count_rows(path: Path) -> int:
    """Rows in a data file: a parquet file's row count from its metadata, a CSV's
    data lines (its records, unless a field holds a line break), or the total
    over the parquet files in a folder."""
    if path.is_dir():
        return sum(count_rows(part) for part in sorted(path.glob("*.parquet")))
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq  # here, so importing this module stays light for the DAG

        return pq.ParquetFile(path).metadata.num_rows
    newlines, last = 0, b"\n"
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    lines = newlines + (last != b"\n")  # a last line without a line break still counts
    return max(lines - 1, 0)  # not the header


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
    files = step_files(step, days, root)
    check_inputs(step, files)
    started = time.monotonic()
    try:
        run_script(SCRIPTS[step], script_args(step, days, root))
        check_outputs(step, files)
        if not days.is_full:
            interim = run_paths(days, root).interim
            for name, column in TRIM.get(step, []):
                before, kept = trim_to_range(interim / name, column, days)
                log.info("run %s task %s: kept %s of %s rows of %s in %s", run_id, step, f"{kept:,}", f"{before:,}", name, days.label)
    except Exception as e:
        log.error("run %s task %s failed: %s: %s", run_id, step, type(e).__name__, e)
        raise
    rows_in = sum(count_rows(path) for path in files.data_in) if files.data_in else None
    rows_out = sum(count_rows(path) for path in files.data_out)
    log_task(run_id, step, rows_in, rows_out, time.monotonic() - started)
    return rows_in, rows_out
