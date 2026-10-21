"""Policy for legacy rows opened after their category's switch date (#53, ADR-021).

map_311.py marks legacy rows opened on or after the first new-system day of
their category (`after_switch`). The processing step reads the policy from
Data-Pipeline/config/processing.csv and applies it with apply_policy:

- keep: the rows count on their day and in later baselines (the default)
- drop: the rows are removed
- exclude_from_baselines: the rows count on their day, but later baselines
  are built without them

Run as a script, it prints the markdown tables behind ADR-021 and writes no
files. The surge flags are analysis only; #16 freezes the label rule.
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from compare_units import STORM_GROUPS, any_group, locate, panel, tract_neighborhoods
from live_baselines import last_full_day, seasonal_baseline, surge_flags
from measure_311 import markdown

SETTING = "after_switch_policy"
POLICIES = ("keep", "drop", "exclude_from_baselines")
CONFIG_COLUMNS = {"setting", "value"}

READ_COLUMNS = ["open_dt", "source_system", "category", "storm_group", "latitude", "longitude", "after_switch"]
OVERLAP_COLUMNS = [
    "category", "storm_group", "switch_date", "last_legacy", "days", "legacy_after_switch", "new_in_window", "legacy_share_pct",
]
FIRST_DAY = "2015-01-01"
WORST = 3  # overlap windows with the most after_switch rows
K, M = 2.0, 3  # the analysis grid point ADR-004 reports; #16 freezes the rule
RATE = f"surge % (k {K:g}, m {M})"
AHEAD_DAYS = 365  # baselines are compared over the year after the data ends


def check_policy(policy: str) -> str:
    if policy not in POLICIES:
        raise ValueError(f"unknown {SETTING} {policy!r}; use one of: {', '.join(POLICIES)}")
    return policy


def load_policy(path: Path | str) -> str:
    """The after_switch policy in the processing config. Raises on a missing, empty or malformed file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    try:
        # utf-8-sig: a file saved from Excel starts with a byte order mark.
        config = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        raise ValueError(f"{path.name} is empty") from None
    except (pd.errors.ParserError, UnicodeDecodeError) as e:
        raise ValueError(f"{path.name} could not be read as a table: {e}") from e
    # pandas turns extra fields in the first row into an index and shifts the columns.
    if not isinstance(config.index, pd.RangeIndex):
        raise ValueError(f"{path.name} could not be read as a table: a row has more fields than the header")
    missing = CONFIG_COLUMNS - set(config.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")
    values = config.loc[config["setting"].str.strip() == SETTING, "value"].str.strip()
    if values.empty:
        raise ValueError(f"{path.name} has no {SETTING} row")
    if len(values) > 1:
        raise ValueError(f"{path.name} sets {SETTING} {len(values)} times")
    return check_policy(values.iloc[0])


def after_switch_flags(df: pd.DataFrame) -> pd.Series:
    """The after_switch column, checked: present, true or false, never missing."""
    if "after_switch" not in df.columns:
        raise ValueError("no after_switch column; rerun map_311.py")
    flags = df["after_switch"]
    if flags.isna().any():
        raise ValueError(f"after_switch is missing on {int(flags.isna().sum())} rows; rerun map_311.py")
    if len(flags) and not pd.api.types.is_bool_dtype(flags):
        raise ValueError(f"after_switch must be true or false, not {flags.dtype}")
    return flags.astype(bool)


def apply_policy(df: pd.DataFrame, policy: str) -> pd.DataFrame:
    """The rows to count under a policy, with in_baseline marking the rows baselines may use."""
    check_policy(policy)
    late = after_switch_flags(df)
    in_baseline = ~late if policy == "exclude_from_baselines" else pd.Series(True, index=df.index)
    out = df.assign(in_baseline=in_baseline)
    return out[~late] if policy == "drop" else out


def overlap_table(df: pd.DataFrame) -> pd.DataFrame:
    """Categories with after_switch rows: the window from the switch to the last such row, and both systems' rows in it.

    The switch is the category's first new-system day, as in map_311.change_points.
    """
    late = after_switch_flags(df)
    new = df[df["source_system"] == "new"]
    rows = []
    for category, legacy in df[late].groupby("category"):
        opened = new.loc[new["category"] == category, "open_dt"].dt.normalize()
        if opened.empty:
            raise ValueError(f"{category} has after_switch rows but no new-system rows; rerun map_311.py")
        switch, last = opened.min(), legacy["open_dt"].max().normalize()
        in_window = int(opened.between(switch, last).sum())
        rows.append({
            "category": category,
            "storm_group": ", ".join(sorted(set(legacy["storm_group"].dropna()) - {""})),
            "switch_date": switch.date(),
            "last_legacy": last.date(),
            "days": (last - switch).days + 1,
            "legacy_after_switch": len(legacy),
            "new_in_window": in_window,
            "legacy_share_pct": round(100 * len(legacy) / (len(legacy) + in_window), 1),
        })
    table = pd.DataFrame(rows, columns=OVERLAP_COLUMNS)
    return table.sort_values(["legacy_after_switch", "category"], ascending=[False, True], ignore_index=True)


def policy_panels(rows: pd.DataFrame, policy: str, level: str, by_group: bool, units, days) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily counts under a policy, and the counts its baselines are built from."""
    kept = apply_policy(rows, policy)
    return panel(kept, level, by_group, units, days), panel(kept[kept["in_baseline"]], level, by_group, units, days)


def surge_cells(counts: pd.DataFrame, base_counts: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Analysis surge flags, the has-baseline mask and the baseline mean of every column (ADR-005, method A)."""
    flags, labeled, expected = {}, {}, {}
    for column in counts.columns:
        base = seasonal_baseline(base_counts[column])
        flags[column] = surge_flags(counts[column], base, K, M)
        labeled[column] = base["mean"].notna()
        expected[column] = base["mean"]
    frames = tuple(pd.DataFrame(f, index=counts.index) for f in (flags, labeled, expected))
    for frame in frames:
        frame.columns = counts.columns
    return frames


def _unit_total(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.T.groupby(level=0).sum(min_count=1).T


def by_unit(counts: pd.DataFrame, flags: pd.DataFrame, labeled: pd.DataFrame, expected: pd.DataFrame) -> tuple:
    """Storm group columns collapsed to one cell per unit and day, which surges when any group does (ADR-004)."""
    return _unit_total(counts), any_group(flags), any_group(labeled), _unit_total(expected)


def window_summary(counts, flags, labeled, expected, start, end) -> dict:
    """Request level against the baseline level, and the surge rate, over one window."""
    c, f = counts.loc[start:end].to_numpy(), flags.loc[start:end]
    lab, e = labeled.loc[start:end].to_numpy(), expected.loc[start:end].to_numpy(dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # an empty window or one with no baseline
        level, base_level = float(np.mean(c)), float(np.nanmean(e))
    return {
        "days": len(f),
        "requests": int(c.sum()),
        "per neighborhood-day": round(level, 3),
        "same weeks, previous 3 years": round(base_level, 3),
        "ratio": round(level / base_level, 2) if base_level else float("nan"),
        "labeled cells": int(lab.sum()),
        RATE: round(100 * int(f.to_numpy().sum()) / max(int(lab.sum()), 1), 2),
        "neighborhoods with a surge": int(f.any(axis=0).sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", default="Data-Pipeline/data/interim/311_combined.parquet")
    parser.add_argument("--config", default="Data-Pipeline/config/processing.csv")
    parser.add_argument("--tracts", default="Data-Pipeline/data/interim/static/tracts_2020.parquet")
    parser.add_argument("--neighborhoods", default="Data-Pipeline/data/interim/static/neighborhoods_by_tract20.parquet")
    args = parser.parse_args()

    policy = load_policy(args.config)
    combined = pd.read_parquet(args.combined, columns=READ_COLUMNS)
    late = after_switch_flags(combined)
    overlap = overlap_table(combined)
    last_day = last_full_day(combined)
    days = pd.date_range(FIRST_DAY, last_day, freq="D")
    legacy_rows = int((combined["source_system"] == "legacy").sum())
    storm = combined["storm_group"].isin(STORM_GROUPS)

    print("## Data\n")
    print(f"- {SETTING} in {args.config}: {policy}")
    print(
        f"- after_switch rows: {int(late.sum()):,} of {legacy_rows:,} legacy rows ({100 * late.sum() / legacy_rows:.3f}%) "
        f"in {len(overlap)} categories, {int((overlap['legacy_after_switch'] > 1).sum())} with more than one row; "
        f"windows up to {overlap['days'].max()} days; storm-related: {int((late & storm).sum())}"
    )
    print(f"- days {days[0].date()} to {last_day.date()} (the last full day of both systems)\n")

    print("## After_switch rows by category\n")
    print("Window: the category's first new-system day to its last legacy row. new_in_window: new-system rows of the category in the same days.\n")
    print(markdown(overlap) + "\n")

    tracts = gpd.read_parquet(args.tracts)
    hoods = tract_neighborhoods(tracts, gpd.read_parquet(args.neighborhoods))
    units = hoods.dropna().unique()
    picked = pd.concat([overlap.head(WORST), overlap[overlap["storm_group"] != ""]]).drop_duplicates("category")
    wanted = storm | combined["category"].isin(picked["category"])
    located, stats = locate(combined[wanted], tracts, hoods)
    placed = located["neighborhood"].notna()
    print(
        f"Placed by coordinates (ADR-004): {int(placed.sum()):,} of {stats['rows']:,} storm and window rows; "
        f"after_switch rows placed: {int(located.loc[placed, 'after_switch'].sum())} of {int(late[wanted].sum())}.\n"
    )

    print(f"## The {WORST} worst overlap windows (most after_switch rows) and the storm one\n")
    print(
        "Per-neighborhood daily counts of the category. Before: the same number of days just before the switch; "
        "no after_switch row falls in it, so it is the same under every policy. "
        "Ratio: per neighborhood-day over the same weeks of the previous 3 years (the ADR-005 baseline mean).\n"
    )
    rows = []
    for item in picked.itertuples(index=False):
        switch, last = pd.Timestamp(item.switch_date), min(pd.Timestamp(item.last_legacy), last_day)
        n = (last - switch).days + 1
        windows = {"before": (switch - pd.Timedelta(days=n), switch - pd.Timedelta(days=1)), "after": (switch, last)}
        requests = located[located["category"] == item.category]
        for name in POLICIES:
            counts, base_counts = policy_panels(requests, name, "neighborhood", False, units, days)
            cells = surge_cells(counts, base_counts)
            for label, (start, end) in windows.items():
                if label == "before" and name != "keep":
                    continue
                rows.append({
                    "category": item.category,
                    "window": f"{label}, {start.date()} to {end.date()}",
                    "policy": name if label == "after" else "all",
                    **window_summary(counts, *cells, start, end),
                })
    print(markdown(pd.DataFrame(rows)) + "\n")

    print("## Storm surge labels by policy: neighborhood x day, any storm group (ADR-004)\n")
    # Panels run a year past the data so the baselines of the coming year can be compared.
    # A baseline only looks 351 days or more back, so the padding cannot reach a day in the data.
    ahead = pd.date_range(FIRST_DAY, last_day + pd.Timedelta(days=AHEAD_DAYS), freq="D")
    windows = {"all days": (days[0], last_day)}
    for item in picked[picked["storm_group"] != ""].itertuples(index=False):
        windows[f"{item.category} window"] = (pd.Timestamp(item.switch_date), min(pd.Timestamp(item.last_legacy), last_day))
    storm_rows = located[located["storm_group"].isin(STORM_GROUPS)]
    results = {}
    for name in POLICIES:
        counts, base_counts = policy_panels(storm_rows, name, "neighborhood", True, units, ahead)
        results[name] = (counts, *surge_cells(counts, base_counts))
    keep_flags = any_group(results["keep"][1])
    rows = []
    for name, (counts, flags, labeled, expected) in results.items():
        unit = by_unit(counts, flags, labeled, expected)
        for label, (start, end) in windows.items():
            rows.append({
                "policy": name,
                "window": f"{label}, {start.date()} to {end.date()}",
                **window_summary(*unit, start, end),
                "labels changed vs keep": int((unit[1].loc[start:end] != keep_flags.loc[start:end]).to_numpy().sum()),
            })
    print(markdown(pd.DataFrame(rows)) + "\n")

    coming = slice(last_day + pd.Timedelta(days=1), None)
    keep_mean = results["keep"][3].loc[coming]
    change = {name: float((r[3].loc[coming] - keep_mean).abs().max().max()) for name, r in results.items() if name != "keep"}
    print(
        f"Coming year ({(last_day + pd.Timedelta(days=1)).date()} to {ahead[-1].date()}): the largest change against keep "
        "in any neighborhood and storm group baseline mean, in requests a day: "
        + ", ".join(f"{name} {value:.3f}" for name, value in change.items()) + "."
    )


if __name__ == "__main__":
    main()
