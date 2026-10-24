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
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # /opt/airflow/repo in the Airflow containers
FIRST_DAY = date(2015, 1, 1)  # first day of the training years (train 2015 to 2023)
LEGACY_YEARS = range(2015, 2027)  # yearly legacy 311 files, as in legacy_311.py
PARAMS = ("start", "end")

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


def run_step(step: str, days: DateRange, root: Path = REPO) -> None:
    run_script(SCRIPTS[step], script_args(step, days, root))
    if days.is_full:
        return
    interim = run_paths(days, root).interim
    for name, column in TRIM.get(step, []):
        before, kept = trim_to_range(interim / name, column, days)
        print(f"kept {kept:,} of {before:,} rows of {name} in {days.label}")
