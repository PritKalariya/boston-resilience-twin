"""Parse the new-system 311 export (BCS case ids) from Analyze Boston.

Same row accounting as legacy_311.py: every dropped row is counted with a reason.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from legacy_311 import ParseReport, scan_records

KEEP_COLUMNS = [
    "case_id",
    "open_date",
    "close_date",
    "case_status",
    "service_name",
    "assigned_department",
    "neighborhood",
    "ward",
    "precinct",
    "zip_code",
    "latitude",
    "longitude",
    "report_source",
]
REQUIRED_COLUMNS = {"case_id", "open_date", "service_name"}

# open_date is published with a +00 suffix, but the wall-clock time is
# Boston local. Cases resubmitted from the legacy system open within minutes
# of the legacy close time (local) in both EST and EDT; true UTC would put
# them 4 to 5 hours later (ADR-020). map_311.check_clock re-tests this every run.
# Set to "utc" if the city starts publishing real UTC.
CLOCK = "local"
BOSTON = "America/New_York"


def to_boston(values: pd.Series, clock: str = CLOCK) -> pd.Series:
    """Naive Boston wall-clock timestamps from the published strings."""
    parsed = pd.to_datetime(values, format="ISO8601", utc=True, errors="coerce")
    if clock == "utc":
        parsed = parsed.dt.tz_convert(BOSTON)
    elif clock != "local":
        raise ValueError(f"unknown clock {clock!r}")
    return parsed.dt.tz_localize(None)


def has_other_offset(values: pd.Series) -> pd.Series:
    stripped = values.str.strip()
    offset = stripped.str.extract(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?([+-]\d{2}(?::?\d{2})?)$")[0]
    return offset.notna() & ~offset.isin(["+00", "+0000", "+00:00", "-00", "-0000", "-00:00"])


def read_new_system(path: Path | str, clock: str = CLOCK) -> tuple[pd.DataFrame, ParseReport]:
    path = Path(path)
    report = ParseReport(source=path.name)

    if path.stat().st_size == 0:
        return pd.DataFrame(columns=KEEP_COLUMNS), report

    raw = pd.read_csv(
        path,
        usecols=lambda c: c in KEEP_COLUMNS,
        dtype=str,
        keep_default_na=False,
        na_values=[""],
    )
    report.file_rows, bad = scan_records(path)
    if len(raw) == report.file_rows:
        raw = raw.drop(index=bad)
    report.drop("malformed_line", report.file_rows - len(raw))

    missing = REQUIRED_COLUMNS - set(raw.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")

    df = raw.reindex(columns=KEEP_COLUMNS)

    no_id = df["case_id"].isna()
    report.drop("missing_case_id", no_id.sum())
    df = df[~no_id]

    # A different offset would mean the export changed; don't guess what it means.
    other_offset = has_other_offset(df["open_date"])
    report.drop("unexpected_offset", other_offset.sum())
    df = df[~other_offset]

    df["open_date"] = to_boston(df["open_date"], clock)
    df["close_date"] = to_boston(df["close_date"], clock)
    bad_open = df["open_date"].isna()
    report.drop("bad_open_date", bad_open.sum())
    df = df[~bad_open]

    dupes = df["case_id"].duplicated(keep="first")
    report.drop("duplicate_case_id", dupes.sum())
    df = df[~dupes]

    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    report.parsed_rows = len(df)
    return df.reset_index(drop=True), report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", default="Data-Pipeline/data/raw/311/new_system.csv")
    parser.add_argument("--out", default="Data-Pipeline/data/interim/new_311.parquet")
    args = parser.parse_args()

    df, report = read_new_system(args.raw)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(pd.DataFrame([report.as_row()]).fillna(0).to_string(index=False))
    print(f"wrote {len(df):,} rows to {out}")


if __name__ == "__main__":
    main()
