"""Parse the yearly legacy 311 CSVs from Analyze Boston.

Every dropped row is counted with a reason so the loss against the
published file can be reported instead of guessed.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

KEEP_COLUMNS = [
    "case_enquiry_id",
    "open_dt",
    "closed_dt",
    "case_status",
    "reason",
    "type",
    "department",
    "neighborhood",
    "ward",
    "precinct",
    "location_zipcode",
    "latitude",
    "longitude",
    "source",
]
REQUIRED_COLUMNS = {"case_enquiry_id", "open_dt", "type"}
OUTPUT_COLUMNS = KEEP_COLUMNS + ["resubmitted_as"]

# Cases moved to the new system are closed with "Resubmitted as BCS-...".
# The new case is a copy, so the combined table drops the legacy one. A few
# ids are typed with the wrong number of digits; real ids have 8.
RESUBMITTED = r"BCS-(\d+)"

# The legacy files mix "%Y-%m-%d %H:%M:%S" with 1 to 3 fractional digits.
# Letting pandas infer the format from the first row turns every other
# variant into NaT, which is the likely cause of the first-run loss.
TIMESTAMP_FORMAT = "ISO8601"


@dataclass
class ParseReport:
    """What parsing one file did: rows in the file, rows kept and rows dropped per reason."""
    source: str
    file_rows: int = 0
    parsed_rows: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str, n: int) -> None:
        """Count n rows dropped for reason; zero leaves no entry."""
        if n:
            self.dropped[reason] = self.dropped.get(reason, 0) + int(n)

    @property
    def dropped_rows(self) -> int:
        """Rows dropped for any reason."""
        return sum(self.dropped.values())

    def as_row(self) -> dict:
        """The report as one table row: source, file and parsed rows, and a dropped_<reason> column per
        reason."""
        return {
            "source": self.source,
            "file_rows": self.file_rows,
            "parsed_rows": self.parsed_rows,
            "dropped_rows": self.dropped_rows,
            **{f"dropped_{k}": v for k, v in sorted(self.dropped.items())},
        }


def scan_records(path: Path) -> tuple[int, list[int]]:
    """Count CSV records and find the ones whose field count differs from the header.

    Uses a real CSV reader so quoted newlines are not split. pandas does not
    flag extra fields once usecols is set, so this is the malformed-row check.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return 0, []
        records, bad = 0, []
        for fields in reader:
            if not fields:
                continue
            if len(fields) != len(header):
                bad.append(records)
            records += 1
    return records, bad


def count_records(path: Path) -> int:
    """Records in a CSV file, counted as scan_records counts them."""
    return scan_records(path)[0]


def parse_timestamps(values: pd.Series) -> pd.Series:
    """Times in TIMESTAMP_FORMAT; any other value becomes NaT."""
    return pd.to_datetime(values, format=TIMESTAMP_FORMAT, errors="coerce")


def read_legacy(path: Path | str, year: int | None = None) -> tuple[pd.DataFrame, ParseReport]:
    """Parse one legacy 311 file. Malformed lines and rows with no case id, no readable open time, a repeated
    case id or (with year) an open time in another year are dropped and counted in the report. An empty
    file gives no rows."""
    path = Path(path)
    report = ParseReport(source=path.name)

    if path.stat().st_size == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), report

    raw = pd.read_csv(
        path,
        usecols=lambda c: c in KEEP_COLUMNS or c == "closure_reason",
        dtype=str,
        keep_default_na=False,
        na_values=[""],
        on_bad_lines="warn",
    )
    report.file_rows, bad = scan_records(path)
    if len(raw) == report.file_rows:
        raw = raw.drop(index=bad)
    report.drop("malformed_line", report.file_rows - len(raw))

    missing = REQUIRED_COLUMNS - set(raw.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")

    df = raw.reindex(columns=KEEP_COLUMNS + ["closure_reason"])

    no_id = df["case_enquiry_id"].isna()
    report.drop("missing_case_id", no_id.sum())
    df = df[~no_id]

    df["open_dt"] = parse_timestamps(df["open_dt"])
    df["closed_dt"] = parse_timestamps(df["closed_dt"])
    bad_open = df["open_dt"].isna()
    report.drop("bad_open_dt", bad_open.sum())
    df = df[~bad_open]

    if year is not None:
        wrong_year = df["open_dt"].dt.year != year
        report.drop("open_dt_outside_file_year", wrong_year.sum())
        df = df[~wrong_year]

    dupes = df["case_enquiry_id"].duplicated(keep="first")
    report.drop("duplicate_case_id", dupes.sum())
    df = df[~dupes]

    digits = df.pop("closure_reason").astype("string").str.extract(RESUBMITTED)[0]
    df["resubmitted_as"] = "BCS-" + digits.str.lstrip("0").str.zfill(8)
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    report.parsed_rows = len(df)
    return df.reset_index(drop=True), report


def read_years(raw_dir: Path | str, years: list[int]) -> tuple[pd.DataFrame, list[ParseReport]]:
    """Parse each year's file and join them, dropping a case id already seen in an earlier year."""
    raw_dir = Path(raw_dir)
    frames, reports = [], []
    for year in years:
        df, report = read_legacy(raw_dir / f"{year}.csv", year=year)
        frames.append(df)
        reports.append(report)

    combined = pd.concat(frames, ignore_index=True)
    cross_year = combined["case_enquiry_id"].duplicated(keep="first")
    if cross_year.any():
        reports[-1].drop("duplicate_case_id_across_years", cross_year.sum())
        combined = combined[~cross_year].reset_index(drop=True)
    return combined, reports


def main() -> None:
    """Parse the yearly legacy files to legacy_311.parquet and print the rows dropped per file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/311")
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2015, 2027)))
    parser.add_argument("--out", default="Data-Pipeline/data/interim/legacy_311.parquet")
    args = parser.parse_args()

    df, reports = read_years(args.raw_dir, args.years)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(pd.DataFrame([r.as_row() for r in reports]).fillna(0).to_string(index=False))
    print(f"wrote {len(df):,} rows to {out}")


if __name__ == "__main__":
    main()
