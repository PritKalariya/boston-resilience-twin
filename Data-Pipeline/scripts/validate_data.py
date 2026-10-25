"""Great Expectations suites on raw and processed 311 data (#17).

--stage raw checks the files in Data-Pipeline/data/raw/311 before parsing:
each legacy yearly file (one at a time, to keep memory low) and the
new-system export. --stage processed checks 311_combined.parquet and
311_located.parquet. Bounds come from Data-Pipeline/config/validation_bounds.csv.

Every table gets two batches: its header (a 0-row frame, so a missing column
fails by name without reading the data) and the columns the checks need.
The run writes every result to --out as JSON and raises ValidationFailed,
naming each failed check, if any check fails, so the DAG task fails and the
steps after it do not run.
"""

from __future__ import annotations

import os

# Great Expectations sends usage analytics unless told not to.
os.environ.setdefault("GX_ANALYTICS_ENABLED", "False")

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import geopandas as gpd
import great_expectations as gx
import great_expectations.expectations as gxe
import pandas as pd

import legacy_311
import map_311
import new_311
from spatial_join import LOCATIONS
from static_layers import BOSTON_BOUNDS

FIRST_DAY = pd.Timestamp("2015-01-01")
STORM_GROUPS = ["snow_ice", "tree", "flooding"]
SYSTEMS = ["legacy", "new"]
MATCHES = ["same", "renamed", "merged", "split", "legacy_only", "new_only", "unmapped", "excluded"]
BOUND_NAMES = {
    "open_dt_parsed_min", "new_offset_suffix_min", "median_open_hour_min", "median_open_hour_max",
    "published_rows_min", "in_boston_min", "placed_per_year_min",
}
GEOID20 = r"^25025\d{6}$"

logging.getLogger("great_expectations").setLevel(logging.WARNING)


class ValidationFailed(RuntimeError):
    """One or more checks failed. The message names each failed check."""


@dataclass
class Check:
    table: str
    expectation: str
    column: str
    success: bool
    observed: str


def load_bounds(path: Path | str) -> dict[str, float]:
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not {"name", "value"} <= set(table.columns):
        raise ValueError(f"{Path(path).name} needs name and value columns")
    values = pd.to_numeric(table["value"], errors="coerce")
    if values.isna().any():
        raise ValueError(f"{Path(path).name} has a value that is not a number: {sorted(table.loc[values.isna(), 'name'])}")
    bounds = dict(zip(table["name"], values.astype(float)))
    missing = BOUND_NAMES - set(bounds)
    if missing:
        raise ValueError(f"{Path(path).name} is missing bounds: {sorted(missing)}")
    return bounds


class Validator:
    """Runs suites on dataframes in one ephemeral Great Expectations context and collects the results."""

    def __init__(self) -> None:
        context = gx.get_context(mode="ephemeral")
        context.variables.progress_bars = {"globally": False}
        self._batches = context.data_sources.add_pandas("frames").add_dataframe_asset("frame").add_batch_definition_whole_dataframe("whole")
        self.checks: list[Check] = []

    def run(self, table: str, df: pd.DataFrame, expectations: list) -> None:
        suite = gx.ExpectationSuite(name=table)
        for expectation in expectations:
            suite.add_expectation(expectation)
        result = self._batches.get_batch(batch_parameters={"dataframe": df}).validate(suite, result_format="SUMMARY")
        for item in result.results:
            config = item.expectation_config
            observed = (item.result or {}).get("observed_value", (item.result or {}).get("unexpected_percent", ""))
            if item.exception_info and item.exception_info.get("raised_exception"):
                observed = item.exception_info.get("exception_message", "error")
            self.checks.append(Check(
                table=table,
                expectation=config.type,
                column=str(config.kwargs.get("column", "")),
                success=bool(item.success),
                observed=str(observed),
            ))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.success]


def header_checks(columns, expected) -> tuple[pd.DataFrame, list]:
    """A 0-row frame with the file's columns and one existence check per expected column."""
    return pd.DataFrame(columns=list(columns)), [gxe.ExpectColumnToExist(column=c) for c in expected]


def open_hours(times: pd.Series) -> pd.Series:
    """Hours after midnight as a fraction (13.5 is 13:30)."""
    return times.dt.hour + times.dt.minute / 60


def in_boston(lat: pd.Series, lon: pd.Series) -> pd.Series:
    west, south, east, north = BOSTON_BOUNDS
    lat, lon = pd.to_numeric(lat, errors="coerce"), pd.to_numeric(lon, errors="coerce")
    return lat.between(south, north) & lon.between(west, east)


def raw_frame(path: Path, time_column: str, keep: list[str], utc: bool) -> tuple[list[str], pd.DataFrame]:
    """The file's header and a frame with the checked columns plus derived ones."""
    header = list(pd.read_csv(path, nrows=0).columns)
    df = pd.read_csv(path, usecols=[c for c in keep if c in header], dtype=str, keep_default_na=False, na_values=[""])
    if time_column in df:
        times = pd.to_datetime(df[time_column], format="ISO8601", errors="coerce", utc=utc)
        df["open_parsed"] = times.notna()
        df["open_hour"] = open_hours(times)
    if {"latitude", "longitude"} <= set(df.columns):
        df["in_boston"] = in_boston(df["latitude"], df["longitude"])
    return header, df


def validate_raw(v: Validator, raw_dir: Path, years: list[int], published: pd.DataFrame, bounds: dict) -> None:
    published = published.set_index("source")["published_rows"]
    hour = {"min_value": bounds["median_open_hour_min"], "max_value": bounds["median_open_hour_max"]}

    for year in years:
        name = f"{year}.csv"
        path = raw_dir / name
        if not path.exists() or path.stat().st_size == 0:
            v.checks.append(Check(f"raw {name}", "file_exists_and_is_not_empty", "", False, "missing or empty"))
            continue
        header, df = raw_frame(path, "open_dt", ["case_enquiry_id", "open_dt", "latitude", "longitude"], utc=False)
        frame, exist = header_checks(header, legacy_311.KEEP_COLUMNS)
        v.run(f"raw {name} header", frame, exist)
        checks = [
            gxe.ExpectColumnValuesToNotBeNull(column="case_enquiry_id"),
            gxe.ExpectColumnValuesToBeUnique(column="case_enquiry_id"),
            gxe.ExpectColumnValuesToBeInSet(column="open_parsed", value_set=[True], mostly=bounds["open_dt_parsed_min"]),
            gxe.ExpectColumnMedianToBeBetween(column="open_hour", **hour),
            gxe.ExpectColumnValuesToBeInSet(column="in_boston", value_set=[True], mostly=bounds["in_boston_min"]),
        ]
        if name in published.index:
            checks.append(gxe.ExpectTableRowCountToBeBetween(min_value=int(bounds["published_rows_min"] * published[name])))
        v.run(f"raw {name}", df, checks)

    name = "new_system.csv"
    path = raw_dir / name
    if not path.exists() or path.stat().st_size == 0:
        v.checks.append(Check(f"raw {name}", "file_exists_and_is_not_empty", "", False, "missing or empty"))
        return
    header, df = raw_frame(path, "open_date", ["case_id", "open_date", "latitude", "longitude"], utc=True)
    frame, exist = header_checks(header, new_311.KEEP_COLUMNS)
    v.run(f"raw {name} header", frame, exist)
    if "open_date" in df:
        df["offset_00"] = df["open_date"].str.strip().str.match(r".*\d{2}:\d{2}(:\d{2}(\.\d+)?)?[+-]00(:?00)?$")
    checks = [
        gxe.ExpectColumnValuesToNotBeNull(column="case_id"),
        gxe.ExpectColumnValuesToBeUnique(column="case_id"),
        gxe.ExpectColumnValuesToBeInSet(column="open_parsed", value_set=[True], mostly=bounds["open_dt_parsed_min"]),
        gxe.ExpectColumnValuesToBeInSet(column="offset_00", value_set=[True], mostly=bounds["new_offset_suffix_min"]),
        gxe.ExpectColumnMedianToBeBetween(column="open_hour", **hour),
        gxe.ExpectColumnValuesToBeInSet(column="in_boston", value_set=[True], mostly=bounds["in_boston_min"]),
    ]
    v.run(f"raw {name}", df, checks)


def parquet_frame(path: Path, keep: list[str]) -> tuple[list[str], pd.DataFrame]:
    import pyarrow.parquet as pq

    header = pq.read_schema(path).names
    return header, pd.read_parquet(path, columns=[c for c in keep if c in header])


def validate_processed(
    v: Validator, combined_path: Path, located_path: Path, units: list[str], latest: date, bounds: dict
) -> None:
    hour = {"min_value": bounds["median_open_hour_min"], "max_value": bounds["median_open_hour_max"]}
    for path in (combined_path, located_path):
        if not path.exists():
            v.checks.append(Check(path.name, "file_exists", "", False, "missing"))
    if v.failed:
        return

    header, df = parquet_frame(combined_path, ["case_id", "source_system", "open_dt", "storm_group", "match"])
    frame, exist = header_checks(header, map_311.COLUMNS)
    v.run("311_combined header", frame, exist)
    checks = [
        gxe.ExpectTableRowCountToBeBetween(min_value=1),
        gxe.ExpectColumnValuesToNotBeNull(column="case_id"),
        gxe.ExpectColumnValuesToBeUnique(column="case_id"),
        gxe.ExpectColumnValuesToBeInSet(column="source_system", value_set=SYSTEMS),
        gxe.ExpectColumnValuesToBeInSet(column="storm_group", value_set=STORM_GROUPS),
        gxe.ExpectColumnValuesToBeInSet(column="match", value_set=MATCHES),
        gxe.ExpectColumnValuesToNotBeNull(column="open_dt"),
        gxe.ExpectColumnValuesToBeBetween(
            column="open_dt", min_value=FIRST_DAY, max_value=pd.Timestamp(latest) + pd.Timedelta(days=1)
        ),
    ]
    v.run("311_combined", df, checks)
    if {"open_dt", "source_system"} <= set(df.columns):
        for system in SYSTEMS:
            part = df.loc[df["source_system"] == system, ["open_dt"]]
            if len(part):
                v.run(f"311_combined {system}", pd.DataFrame({"open_hour": open_hours(part["open_dt"])}),
                      [gxe.ExpectColumnMedianToBeBetween(column="open_hour", **hour)])
    combined_rows = len(df)
    del df

    header, df = parquet_frame(located_path, ["open_dt", "geoid20", "unit", "location"])
    frame, exist = header_checks(header, [*map_311.COLUMNS, "geoid20", "unit", "location"])
    v.run("311_located header", frame, exist)
    v.run("311_located", df, [
        gxe.ExpectTableRowCountToEqual(value=combined_rows),
        gxe.ExpectColumnValuesToNotBeNull(column="location"),
        gxe.ExpectColumnValuesToBeInSet(column="location", value_set=LOCATIONS),
        gxe.ExpectColumnValuesToBeInSet(column="unit", value_set=units),
        gxe.ExpectColumnValuesToMatchRegex(column="geoid20", regex=GEOID20),
    ])
    if {"open_dt", "location"} <= set(df.columns) and len(df):
        placed = (df["location"] == "placed").groupby(df["open_dt"].dt.year).mean()
        v.run("311_located per year", pd.DataFrame({"year": placed.index, "placed_share": placed.to_numpy()}), [
            gxe.ExpectColumnMinToBeBetween(column="placed_share", min_value=bounds["placed_per_year_min"]),
        ])


def markdown(checks: list[Check]) -> str:
    lines = ["| table | check | column | ok | observed |", "|---|---|---|---|---|"]
    lines += [f"| {c.table} | {c.expectation} | {c.column} | {'yes' if c.success else 'NO'} | {c.observed} |" for c in checks]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["raw", "processed"], required=True)
    parser.add_argument("--config", default="Data-Pipeline/config/validation_bounds.csv")
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/311")
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2015, 2027)))
    parser.add_argument("--published", default="Data-Pipeline/config/published_311_counts.csv")
    parser.add_argument("--combined", default="Data-Pipeline/data/interim/311_combined.parquet")
    parser.add_argument("--located", default="Data-Pipeline/data/interim/311_located.parquet")
    parser.add_argument("--neighborhoods", default="Data-Pipeline/data/interim/static/neighborhoods_by_tract20.parquet")
    parser.add_argument("--latest", type=date.fromisoformat, default=date.today(), help="last day a request may open")
    parser.add_argument("--out", help="results JSON (default: Data-Pipeline/data/interim/validation/<stage>.json)")
    args = parser.parse_args()

    bounds = load_bounds(args.config)
    v = Validator()
    if args.stage == "raw":
        validate_raw(v, Path(args.raw_dir), args.years, pd.read_csv(args.published), bounds)
    else:
        units = sorted(gpd.read_parquet(args.neighborhoods)["neighborhood"].dropna().unique())
        validate_processed(v, Path(args.combined), Path(args.located), units, args.latest, bounds)

    out = Path(args.out or f"Data-Pipeline/data/interim/validation/{args.stage}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"stage": args.stage, "checks": [asdict(c) for c in v.checks]}, indent=2))
    print(markdown(v.checks))
    print(f"\n{len(v.checks) - len(v.failed)} of {len(v.checks)} checks passed; wrote {out}")
    if v.failed:
        names = "; ".join(f"{c.table}: {c.expectation}({c.column}) observed {c.observed}" for c in v.failed)
        raise ValidationFailed(f"{len(v.failed)} {args.stage} checks failed: {names}")


if __name__ == "__main__":
    main()
