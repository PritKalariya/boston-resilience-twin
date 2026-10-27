"""Daily storm-related 311 counts per neighborhood (#15, ADR-004).

Reads 311_located.parquet from spatial_join.py, applies the after_switch policy
(ADR-021) and writes daily_counts.parquet: one row per unit, day and storm
group, with every day present and zero-filled. `requests` counts every kept
request; `baseline_requests` counts the ones baselines may use (ADR-005), which
under the default policy is the same. The run prints how the total reconciles
with the combined table and stops if it does not.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from after_switch import apply_policy, load_policy

STORM_GROUPS = ["snow_ice", "tree", "flooding"]
FIRST_DAY = "2015-01-01"
PLACED = "placed"
UNMAPPED = "unmapped"
REQUIRED_COLUMNS = {"open_dt", "source_system", "category", "storm_group", "location", "unit", "after_switch"}
COLUMNS = ["unit", "date", "storm_group", "requests", "baseline_requests"]


def last_full_day(df: pd.DataFrame) -> pd.Timestamp:
    """The day before the earlier of the two systems' last request."""
    return df.groupby("source_system")["open_dt"].max().min().normalize() - pd.Timedelta(days=1)


def check(df: pd.DataFrame, units: list[str]) -> None:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"located table is missing columns: {sorted(missing)}; rerun spatial_join.py")
    groups = set(df["storm_group"].dropna()) - set(STORM_GROUPS)
    if groups:
        raise ValueError(f"unknown storm groups {sorted(groups)}; check docs/data/category-map.csv")
    placed = df["location"] == PLACED
    if df.loc[placed, "unit"].isna().any():
        raise ValueError("placed rows without a unit; rerun spatial_join.py")
    strange = set(df.loc[placed, "unit"]) - set(units)
    if strange:
        raise ValueError(f"units {sorted(strange)} are not in the neighborhood layer")


def daily_counts(
    df: pd.DataFrame, units: list[str], policy: str, start, end
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Zero-filled counts per unit, day and storm group, and the steps from every row to the counted ones."""
    check(df, units)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    steps = [("rows in the combined table", len(df))]

    def keep(mask: pd.Series, reason: str) -> pd.DataFrame:
        steps.append((reason, -int((~mask).sum())))
        return df[mask]

    df = keep(df["category"] != UNMAPPED, "unmapped category")
    df = keep(df["storm_group"].isin(STORM_GROUPS), "not storm-related")
    kept = apply_policy(df, policy)
    steps.append((f"after_switch rows removed ({policy})", len(kept) - len(df)))
    df = kept
    for reason in ("no_coordinates", "placeholder", "outside_boston", "no_neighborhood"):
        df = keep(df["location"] != reason, f"no location: {reason}")
    day = df["open_dt"].dt.normalize()
    df = keep(day.notna(), "no open date")
    day = df["open_dt"].dt.normalize()
    df = keep(day.between(start, end), f"outside {start.date()} to {end.date()}")
    steps.append(("counted", len(df)))

    days = pd.date_range(start, end, freq="D")
    full = pd.MultiIndex.from_product([sorted(units), days, STORM_GROUPS], names=["unit", "date", "storm_group"])
    keys = [df["unit"], df["open_dt"].dt.normalize().rename("date"), df["storm_group"]]
    requests = df.groupby(keys).size().reindex(full, fill_value=0)
    in_base = df["in_baseline"]
    baseline = df[in_base].groupby([k[in_base] for k in keys]).size().reindex(full, fill_value=0)
    counts = pd.DataFrame({"requests": requests, "baseline_requests": baseline}).astype("int64").reset_index()

    report = pd.DataFrame(steps, columns=["step", "requests"])
    if int(counts["requests"].sum()) != len(df):
        raise AssertionError(f"counts total {int(counts['requests'].sum()):,} but {len(df):,} requests were counted")
    return counts[COLUMNS], report


def markdown(df: pd.DataFrame) -> str:
    cells = df.astype(object).where(df.notna(), "").astype(str)
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(row) + " |" for row in cells.itertuples(index=False)]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--located", default="Data-Pipeline/data/interim/311_located.parquet")
    parser.add_argument("--neighborhoods", default="Data-Pipeline/data/interim/static/neighborhoods_by_tract20.parquet")
    parser.add_argument("--config", default="Data-Pipeline/config/processing.csv")
    parser.add_argument("--start", default=FIRST_DAY)
    parser.add_argument("--end", help="last day to count; default: the last full day of both systems")
    parser.add_argument("--out", default="Data-Pipeline/data/processed/daily_counts.parquet")
    args = parser.parse_args()

    policy = load_policy(args.config)
    units = pd.read_parquet(args.neighborhoods, columns=["neighborhood"])["neighborhood"].dropna().unique().tolist()
    located = pd.read_parquet(args.located, columns=sorted(REQUIRED_COLUMNS))
    if args.end:
        end = pd.Timestamp(args.end)
    elif located.empty:
        raise ValueError("no requests to count; pass --end to write an all-zero table")
    else:
        end = last_full_day(located)

    counts, report = daily_counts(located, units, policy, args.start, end)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts.to_parquet(out, index=False)

    empty = 100 * (counts["requests"] == 0).mean()
    print(f"after_switch_policy: {policy}")
    print(markdown(report))
    print(f"\n{len(counts):,} cells ({len(units)} units x {counts['date'].nunique():,} days x {len(STORM_GROUPS)} groups), "
          f"{empty:.1f}% empty; wrote {out}")


if __name__ == "__main__":
    main()
