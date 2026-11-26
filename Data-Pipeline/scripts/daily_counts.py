"""Daily storm-related 311 counts per neighborhood (#15, ADR-004).

Reads 311_located.parquet from spatial_join.py, applies the after_switch policy
(ADR-021) and writes daily_counts.parquet: one row per unit, day and storm
group, with every day present and zero-filled. `requests` counts every kept
request; `baseline_requests` counts the ones baselines may use (ADR-005), which
under the default policy is the same. The run prints how the total reconciles
with the combined table and stops if it does not.

Where the counts end (last_full_day): the day before the earlier of the two
systems' last request, so a day one export has not reached yet never counts as
zero. Once every storm group both systems carry has switched to the new system
(the storm_group rows of combine_311's change points; snow_ice was the last, on
2026-06-23), the legacy system holds none of their categories, so from then on
only the new system's last request sets the end (#160). Flooding is the one
storm group with no new-system category: it counts zero after the legacy
system's last flooding request, and it is in neither the labels nor the 311
features (labels_features.csv).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from after_switch import apply_policy, load_policy

STORM_GROUPS = ["snow_ice", "tree", "flooding"]
FIRST_DAY = "2015-01-01"
NEW_SYSTEM = "new"
PLACED = "placed"
UNMAPPED = "unmapped"
REQUIRED_COLUMNS = {"open_dt", "source_system", "category", "storm_group", "location", "unit", "after_switch"}
COLUMNS = ["unit", "date", "storm_group", "requests", "baseline_requests"]


def last_full_day(df: pd.DataFrame, switch_dates=()) -> pd.Timestamp:
    """The day before the earlier of the two systems' last request or, once that day is after every
    storm group's switch date, the day before the new system's last request (module docstring)."""
    last = df.groupby("source_system")["open_dt"].max().dt.normalize() - pd.Timedelta(days=1)
    switched = pd.to_datetime(pd.Series(list(switch_dates), dtype=object)).max() if len(switch_dates) else None
    if switched is not None and NEW_SYSTEM in last and last[NEW_SYSTEM] > switched:
        return last[NEW_SYSTEM]
    return last.min()


def storm_switch_dates(path: Path | str) -> list[pd.Timestamp]:
    """The switch date of every storm group in combine_311's change points (map_311.change_points)."""
    points = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = {"level", "name", "switch_date"} - set(points.columns)
    if missing:
        raise ValueError(f"{path} is missing columns {sorted(missing)}; rerun map_311.py")
    groups = points[points["level"] == "storm_group"]
    return [pd.Timestamp(day) for day in groups["switch_date"] if day]


def check(df: pd.DataFrame, units: list[str]) -> None:
    """Fail on a located table the counts cannot trust: missing columns, unknown storm groups, or placed rows
    with no unit or a unit not in the neighborhood layer."""
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
    """A table as GitHub markdown, missing values left blank."""
    cells = df.astype(object).where(df.notna(), "").astype(str)
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(row) + " |" for row in cells.itertuples(index=False)]
    return "\n".join(lines)


def main() -> None:
    """Write daily_counts.parquet for --start to --end (default: last_full_day) and print
    how the rows reconcile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--located", default="Data-Pipeline/data/interim/311_located.parquet")
    parser.add_argument("--neighborhoods", default="Data-Pipeline/data/interim/static/neighborhoods_by_tract20.parquet")
    parser.add_argument("--config", default="Data-Pipeline/config/processing.csv")
    parser.add_argument("--change-points", default="Data-Pipeline/data/interim/change-points.csv",
                        help="combine_311's switch dates, which set where the counts end")
    parser.add_argument("--start", default=FIRST_DAY)
    parser.add_argument("--end", help="last day to count; default: last_full_day")
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
        end = last_full_day(located, storm_switch_dates(args.change_points))

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
