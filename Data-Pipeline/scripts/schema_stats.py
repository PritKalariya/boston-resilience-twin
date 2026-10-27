"""Schema and summary statistics of the processed 311 tables, every run (#35).

For each table it writes <table>.json to --out-dir: the schema (columns, types,
share missing) and summary statistics (numeric spread, category shares, rows
per year). It compares them with the previous run's files and writes the shifts
beyond the thresholds in Data-Pipeline/config/stats_thresholds.csv to
drift.json, and builds Great Expectations data docs (data_docs/index.html) that
show the same numbers.

Files are replaced, not appended: DVC keeps the history, and a run whose
statistics equal the previous run's writes nothing, so rerunning on unchanged
data changes nothing. Shifts are flagged, not failed; #17's validation is what
stops a run.
"""

from __future__ import annotations

import os

# Great Expectations sends usage analytics unless told not to.
os.environ.setdefault("GX_ANALYTICS_ENABLED", "False")

import argparse
import json
import logging
import shutil
import tempfile
from pathlib import Path

import pandas as pd

# What each one means is in the note column of Data-Pipeline/config/stats_thresholds.csv.
THRESHOLDS = ("row_count_change_pct", "missing_change_points", "mean_shift_std", "category_share_change_points")
MAX_CATEGORIES = 50  # columns with more distinct values (ids, addresses) get no shares
DATE_COLUMNS = ("open_dt", "date")
# In a count table every unit and group has the same number of rows, so category
# shares are weighted by this column there: a storm group's share of requests.
WEIGHT_COLUMN = "requests"
DIGITS = 4
# Means of tiny tables (a one-day slice) move a lot for no reason, so they are not compared.
MIN_ROWS_FOR_MEAN = 30
# With a constant column (std 0) any change is infinite in std units; flag only a real move.
CONSTANT_MEAN_FLOOR = 0.01

logging.getLogger("great_expectations").setLevel(logging.WARNING)


def load_thresholds(path: Path | str) -> dict[str, float]:
    """The drift thresholds in stats_thresholds.csv (name, value). Raises when a column, a number or a
    threshold is missing."""
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not {"name", "value"} <= set(table.columns):
        raise ValueError(f"{Path(path).name} needs name and value columns")
    values = pd.to_numeric(table["value"], errors="coerce")
    if values.isna().any():
        raise ValueError(f"{Path(path).name} has a value that is not a number: {sorted(table.loc[values.isna(), 'name'])}")
    thresholds = dict(zip(table["name"], values.astype(float)))
    missing = set(THRESHOLDS) - set(thresholds)
    if missing:
        raise ValueError(f"{Path(path).name} is missing thresholds: {sorted(missing)}")
    return thresholds


def _round(value) -> float | None:
    return None if pd.isna(value) else round(float(value), DIGITS)


def table_stats(df: pd.DataFrame) -> dict:
    """Schema and statistics of one table, as plain JSON values."""
    rows = len(df)
    weight = df[WEIGHT_COLUMN] if WEIGHT_COLUMN in df.columns else None
    schema, numeric, categories = {}, {}, {}
    for column in df.columns:
        values = df[column]
        schema[column] = {"dtype": str(values.dtype), "missing_pct": _round(100 * values.isna().mean()) if rows else 0.0}
        if pd.api.types.is_bool_dtype(values) or not (pd.api.types.is_numeric_dtype(values) or pd.api.types.is_datetime64_any_dtype(values)):
            if values.nunique(dropna=True) <= MAX_CATEGORIES:
                labels = values.astype("string").fillna("<missing>")
                if weight is not None and weight.sum():
                    shares = weight.groupby(labels).sum() / weight.sum() * 100
                else:
                    shares = labels.value_counts(normalize=True) * 100
                categories[column] = {str(k): _round(v) for k, v in sorted(shares.items())}
        elif pd.api.types.is_numeric_dtype(values):
            q = values.quantile([0.25, 0.5, 0.75]) if rows else pd.Series([None] * 3, index=[0.25, 0.5, 0.75])
            numeric[column] = {
                "mean": _round(values.mean()), "std": _round(values.std()), "min": _round(values.min()),
                "p25": _round(q[0.25]), "p50": _round(q[0.5]), "p75": _round(q[0.75]), "max": _round(values.max()),
            }
    stats = {"rows": rows, "schema": schema, "numeric": numeric, "categories": categories,
             "category_shares_of": WEIGHT_COLUMN if weight is not None else "rows"}
    date = next((c for c in DATE_COLUMNS if c in df.columns), None)
    if date is not None:
        years = pd.to_datetime(df[date]).dt.year.value_counts().sort_index()
        stats["rows_per_year"] = {str(int(y)): int(n) for y, n in years.items()}
    return stats


def drift(previous: dict, current: dict, thresholds: dict[str, float]) -> list[dict]:
    """Shifts from the previous run beyond the thresholds. Schema changes are always flagged."""
    flags = []

    def flag(kind, column, before, after, detail):
        flags.append({"kind": kind, "column": column, "previous": before, "current": after, "detail": detail})

    before_rows, rows = previous["rows"], current["rows"]
    change = 100 * abs(rows - before_rows) / before_rows if before_rows else (0.0 if rows == 0 else float("inf"))
    if change > thresholds["row_count_change_pct"]:
        flag("row_count", "", before_rows, rows, f"{change:.1f}% change")

    old, new = previous["schema"], current["schema"]
    for column in sorted(set(old) - set(new)):
        flag("column_removed", column, old[column]["dtype"], None, "column is gone")
    for column in sorted(set(new) - set(old)):
        flag("column_added", column, None, new[column]["dtype"], "new column")
    for column in sorted(set(old) & set(new)):
        if old[column]["dtype"] != new[column]["dtype"]:
            flag("dtype_changed", column, old[column]["dtype"], new[column]["dtype"], "type changed")
        moved = abs((new[column]["missing_pct"] or 0) - (old[column]["missing_pct"] or 0))
        if moved > thresholds["missing_change_points"]:
            flag("missing_share", column, old[column]["missing_pct"], new[column]["missing_pct"], f"{moved:.2f} points")
        if column in previous["categories"] and column not in current["categories"]:
            # A corrupted unit or location column would look like this: too many values to have shares.
            flag("categories_lost", column, len(previous["categories"][column]), None, f"now more than {MAX_CATEGORIES} distinct values")

    for column in sorted(set(previous["numeric"]) & set(current["numeric"])):
        a, b = previous["numeric"][column], current["numeric"][column]
        if a["mean"] is None or b["mean"] is None or before_rows < MIN_ROWS_FOR_MEAN:
            continue
        spread = a["std"] or 0.0
        shift = abs(b["mean"] - a["mean"])
        if spread and shift / spread > thresholds["mean_shift_std"]:
            flag("mean_shift", column, a["mean"], b["mean"], f"{shift / spread:.2f} previous std")
        elif not spread and shift > CONSTANT_MEAN_FLOOR * max(abs(a["mean"]), 1.0):
            flag("mean_shift", column, a["mean"], b["mean"], "mean moved and the previous std was 0")

    for column in sorted(set(previous["categories"]) & set(current["categories"])):
        a, b = previous["categories"][column], current["categories"][column]
        for value in sorted(set(a) | set(b)):
            moved = abs(b.get(value, 0.0) - a.get(value, 0.0))
            if moved > thresholds["category_share_change_points"]:
                flag("category_share", f"{column}={value}", a.get(value, 0.0), b.get(value, 0.0), f"{moved:.2f} points")
    return flags


def docs_columns(stats: dict) -> list[str]:
    """The columns the data docs describe: the date column, then the numeric and category columns in stats."""
    date = [c for c in DATE_COLUMNS if c in stats["schema"]]
    return [*date, *stats["numeric"], *stats["categories"]]


def build_data_docs(frames: dict[str, pd.DataFrame], stats: dict[str, dict], site: Path) -> Path:
    """Great Expectations data docs for every table: the expectations only record observed values."""
    import great_expectations as gx
    import great_expectations.expectations as gxe

    # Built next to the old site and swapped in at the end, so a failure leaves the old site in place.
    building = site.with_name(f".{site.name}.building")
    shutil.rmtree(building, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="gx-") as root:
        _render(gx, gxe, root, frames, stats, building)
    shutil.rmtree(site, ignore_errors=True)
    building.rename(site)
    return site / "index.html"


def _render(gx, gxe, root: str, frames: dict[str, pd.DataFrame], stats: dict[str, dict], site: Path) -> None:
    context = gx.get_context(mode="file", project_root_dir=root)
    context.variables.progress_bars = {"globally": False}
    context.add_data_docs_site(site_name="stats", site_config={
        "class_name": "SiteBuilder",
        "store_backend": {"class_name": "TupleFilesystemStoreBackend", "base_directory": str(site.resolve())},
        "site_index_builder": {"class_name": "DefaultSiteIndexBuilder"},
    })
    source = context.data_sources.add_pandas("tables")
    definitions = []
    for name, df in frames.items():
        suite = context.suites.add(gx.ExpectationSuite(name=f"{name} schema and statistics"))
        suite.add_expectation(gxe.ExpectTableRowCountToBeBetween())
        for column in df.columns:
            suite.add_expectation(gxe.ExpectColumnValuesToNotBeNull(column=column, mostly=0.0))
        for column in stats[name]["numeric"]:
            suite.add_expectation(gxe.ExpectColumnMeanToBeBetween(column=column))
            suite.add_expectation(gxe.ExpectColumnMedianToBeBetween(column=column))
            suite.add_expectation(gxe.ExpectColumnMinToBeBetween(column=column))
            suite.add_expectation(gxe.ExpectColumnMaxToBeBetween(column=column))
        for column in stats[name]["categories"]:
            values = sorted(df[column].dropna().unique().tolist(), key=str)
            if values:
                suite.add_expectation(gxe.ExpectColumnDistinctValuesToBeInSet(column=column, value_set=values))
        batch = source.add_dataframe_asset(name).add_batch_definition_whole_dataframe("whole")
        definitions.append((context.validation_definitions.add(gx.ValidationDefinition(name=name, data=batch, suite=suite)), df))
    for definition, df in definitions:
        result = definition.run(batch_parameters={"dataframe": df})
        if not result.success:
            failed = [r.expectation_config.type for r in result.results if not r.success]
            raise RuntimeError(f"data docs checks for {definition.name} did not pass: {failed}")
    context.build_data_docs(site_names=["stats"])


def run(tables: dict[str, Path], thresholds: dict[str, float], out_dir: Path, docs: bool = True) -> dict:
    """Write each table's statistics and the drift flags when anything changed. Returns what happened.

    A table is compared with its previous statistics, or, when those are the same
    as now, with the baseline the previous run compared against (kept in
    drift.json), so a threshold change re-checks the same comparison.
    """
    present = {name: path for name, path in tables.items() if path.exists()}
    skipped = sorted(set(tables) - set(present))
    current, frames = {}, {}
    for name, path in present.items():
        df = pd.read_parquet(path)
        current[name] = table_stats(df)
        # The data docs only need the described columns; the whole table would
        # roughly double the peak inside Great Expectations.
        frames[name] = df[docs_columns(current[name])].copy()
        del df
    previous = {}
    for name in current:
        path = out_dir / f"{name}.json"
        if path.exists():
            previous[name] = json.loads(path.read_text())
    report_path, site = out_dir / "drift.json", out_dir / "data_docs"
    last = json.loads(report_path.read_text()) if report_path.exists() else {}

    same_stats = bool(current) and all(previous.get(name) == stats for name, stats in current.items())
    docs_there = not docs or (site / "index.html").exists()
    if same_stats and last.get("thresholds") == thresholds and docs_there:
        return {"changed": False, "skipped": skipped, "flags": []}

    baselines = {}
    for name, stats in current.items():
        if name in previous and previous[name] != stats:
            baselines[name] = previous[name]
        elif name in last.get("baselines", {}):
            baselines[name] = last["baselines"][name]
    flags = [{"table": name, **f} for name, base in baselines.items() for f in drift(base, current[name], thresholds)]
    first_run = sorted(set(current) - set(baselines))

    # Docs first and the JSON last, so a failed docs build leaves the previous run's files as they were.
    index = build_data_docs(frames, current, site) if docs and frames else None
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, stats in current.items():
        (out_dir / f"{name}.json").write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    report = {"compared": sorted(baselines), "first_run": first_run, "skipped": skipped, "thresholds": thresholds,
              "flags": flags, "baselines": baselines}
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {"changed": True, "skipped": skipped, "flags": flags, "first_run": first_run, "data_docs": index}


def main() -> None:
    """Write each processed table's schema and statistics and the drift flags, as the schema_and_statistics
    task does."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--located", default="Data-Pipeline/data/interim/311_located.parquet")
    parser.add_argument("--daily-counts", default="Data-Pipeline/data/processed/daily_counts.parquet")
    parser.add_argument("--thresholds", default="Data-Pipeline/config/stats_thresholds.csv")
    parser.add_argument("--out-dir", default="Data-Pipeline/data/processed/stats")
    parser.add_argument("--no-docs", action="store_true", help="skip the Great Expectations data docs")
    args = parser.parse_args()

    tables = {"located": Path(args.located), "daily_counts": Path(args.daily_counts)}
    result = run(tables, load_thresholds(args.thresholds), Path(args.out_dir), docs=not args.no_docs)
    for name in result["skipped"]:
        print(f"skipped {name}: {tables[name]} does not exist yet")
    if not result["changed"]:
        print(f"statistics unchanged since the previous run; nothing written to {args.out_dir}")
        return
    for name in result["first_run"]:
        print(f"{name}: first run, nothing to compare with")
    print(f"{len(result['flags'])} shift(s) beyond the thresholds")
    for f in result["flags"]:
        print(f"  {f['table']} {f['kind']} {f['column']}: {f['previous']} -> {f['current']} ({f['detail']})")
    print(f"wrote statistics and drift.json to {args.out_dir}" + (f"; data docs at {result['data_docs']}" if result.get("data_docs") else ""))


if __name__ == "__main__":
    main()
