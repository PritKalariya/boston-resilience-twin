"""Compare candidate modeling units on empty-cell share and surge rate (#10, ADR-004).

Storm-related requests from 311_combined.parquet are placed in 2020 tracts and
in the 24 neighborhoods built from whole 2020 tracts by their coordinates, so
both 311 systems share one unit list. Every candidate unit gets a full panel of
daily counts (zero-filled), the ADR-005 baseline (method A) and the analysis
surge grid. Prints markdown tables for docs/status.md.

The surge rule here is for comparison only; #16 freezes the real one.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from live_baselines import GRID, seasonal_baseline, surge_flags

STORM_GROUPS = ["snow_ice", "tree", "flooding"]
READ_COLUMNS = ["open_dt", "storm_group", "latitude", "longitude"]
FIRST_DAY = "2015-01-01"
WINDOWS = {
    "train 2016 to 2023": ("2016-01-01", "2023-12-31"),
    "2024 to 2026": ("2024-01-01", None),
}
# (name, level, split by storm group, collapse to "any group surged").
# "pooled" units add the groups before the baseline; "any group" units keep a
# baseline per group (ADR-005) and flag the cell when any group surges.
CANDIDATES = [
    ("citywide x day x group", "city", True, False),
    ("neighborhood x day, pooled", "neighborhood", False, False),
    ("neighborhood x day, any group", "neighborhood", True, True),
    ("neighborhood x day x group", "neighborhood", True, False),
    ("tract x day, pooled", "geoid20", False, False),
    ("tract x day, any group", "geoid20", True, True),
    ("tract x day x group", "geoid20", True, False),
]


def tract_neighborhoods(tracts: gpd.GeoDataFrame, neighborhoods: gpd.GeoDataFrame) -> pd.Series:
    """Neighborhood of each 2020 tract, by the tract's representative point.

    The neighborhoods are unions of whole tracts, so each tract falls in at
    most one. The city leaves tract 9815.02 (no population, by Suffolk Downs)
    out of every neighborhood; such tracts map to NaN.
    """
    points = gpd.GeoDataFrame({"geoid20": tracts["geoid20"].values}, geometry=tracts.representative_point().values, crs=tracts.crs)
    joined = gpd.sjoin(points, neighborhoods[["neighborhood", "geometry"]], how="left", predicate="within")
    return joined.drop_duplicates("geoid20").set_index("geoid20")["neighborhood"]


def locate(df: pd.DataFrame, tracts: gpd.GeoDataFrame, neighborhoods: pd.Series) -> tuple[pd.DataFrame, dict]:
    """Add geoid20 and neighborhood from the request coordinates; count what cannot be placed."""
    lat = pd.to_numeric(df["latitude"], errors="coerce")
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    no_coords = lat.isna() | lon.isna()
    usable = ~no_coords
    points = gpd.GeoDataFrame(
        index=df.index[usable], geometry=gpd.points_from_xy(lon[usable], lat[usable]), crs="EPSG:4326"
    ).to_crs(tracts.crs)
    joined = gpd.sjoin(points, tracts[["geoid20", "geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]

    out = df.copy()
    out["geoid20"] = joined["geoid20"].reindex(df.index)
    out["neighborhood"] = out["geoid20"].map(neighborhoods)
    outside = usable & out["geoid20"].isna()
    stats = {
        "rows": len(df),
        "no_coordinates": int(no_coords.sum()),
        "outside_tracts": int(outside.sum()),
    }
    stats["located"] = int(out["geoid20"].notna().sum())
    stats["tract_without_neighborhood"] = int((out["geoid20"].notna() & out["neighborhood"].isna()).sum())
    return out[out["geoid20"].notna()].reset_index(drop=True), stats


def panel(df: pd.DataFrame, level: str, by_group: bool, units, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Daily counts with one column per unit (and storm group), every day present, zeros filled."""
    rows = df.assign(day=df["open_dt"].dt.normalize())
    rows = rows[rows["day"].between(days[0], days[-1])]
    if level == "city":
        rows = rows.assign(city="Boston")
        units = ["Boston"]
    keys = [level, "storm_group"] if by_group else [level]
    counts = rows.groupby(["day"] + keys).size().unstack(keys, fill_value=0)
    if by_group:
        columns = pd.MultiIndex.from_product([sorted(units), STORM_GROUPS], names=keys)
    else:
        columns = pd.Index(sorted(units), name=level)
    return counts.reindex(index=days, columns=columns, fill_value=0).astype("int64")


def flags_for(counts: pd.DataFrame, k: float, m: int, baselines: dict) -> pd.DataFrame:
    """Analysis surge flags per column and the matching has-baseline mask."""
    flags, labeled = {}, {}
    for column in counts.columns:
        base = baselines.get(column)
        if base is None:
            base = baselines[column] = seasonal_baseline(counts[column])
        flags[column] = surge_flags(counts[column], base, k, m)
        labeled[column] = base["mean"].notna()
    return pd.DataFrame(flags), pd.DataFrame(labeled)


def any_group(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse group columns to one column per unit: true when any group is true."""
    return frame.T.groupby(level=0).any().T


def unit_metrics(counts: pd.DataFrame, windows: dict, grid=GRID, collapse: bool = False) -> list[dict]:
    """Empty-cell share and surge rate for one candidate, per window and grid point.

    With `collapse`, counts are split by storm group, each group keeps its own
    baseline, and a unit-day is one cell that surges when any group does.
    """
    baselines: dict = {}
    per_grid = {(k, m): flags_for(counts, k, m, baselines) for k, m in grid}
    if collapse:
        per_grid = {key: (any_group(f), any_group(lab)) for key, (f, lab) in per_grid.items()}
        counts = counts.T.groupby(level=0).sum().T
    rows = []
    for label, (start, end) in windows.items():
        window = counts.loc[start:end]
        years = len(window) / 365.25
        row = {"window": label, "units": counts.shape[1], "cells": int(window.size), "empty_pct": round(100 * float((window == 0).to_numpy().mean()), 1)}
        extra = {}
        for (k, m), (flags, labeled) in per_grid.items():
            f, lab = flags.loc[start:end], labeled.loc[start:end]
            surges = int(f.to_numpy().sum())
            row[f"surge_pct k{k:g} m{m}"] = round(100 * surges / max(int(lab.to_numpy().sum()), 1), 2)
            if (k, m) == (2.0, 3):
                per_unit = f.sum(axis=0) / years
                extra["surges per column-year, k2 m3 (median)"] = round(float(per_unit.median()), 1)
                extra["columns with no surge, k2 m3"] = int((f.sum(axis=0) == 0).sum())
        rows.append({**row, **extra})
    return rows


def group_metrics(counts: pd.DataFrame, windows: dict, k: float = 2.0, m: int = 3) -> list[dict]:
    """Per storm group view of a group-split candidate."""
    rows = []
    for group in STORM_GROUPS:
        sub = counts.xs(group, axis=1, level="storm_group")
        flags, labeled = flags_for(sub, k, m, {})
        for label, (start, end) in windows.items():
            w, f, lab = sub.loc[start:end], flags.loc[start:end], labeled.loc[start:end]
            rows.append({
                "storm_group": group,
                "window": label,
                "empty_pct": round(100 * float((w == 0).to_numpy().mean()), 1),
                f"surge_pct k{k:g} m{m}": round(100 * int(f.to_numpy().sum()) / max(int(lab.to_numpy().sum()), 1), 2),
                "columns with no surge": int((f.sum(axis=0) == 0).sum()),
            })
    return rows


def markdown(df: pd.DataFrame) -> str:
    cells = df.astype(object).where(df.notna(), "").astype(str)
    lines = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(row) + " |" for row in cells.itertuples(index=False)]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", default="Data-Pipeline/data/interim/311_combined.parquet")
    parser.add_argument("--tracts", default="Data-Pipeline/data/interim/static/tracts_2020.parquet")
    parser.add_argument("--neighborhoods", default="Data-Pipeline/data/interim/static/neighborhoods_by_tract20.parquet")
    args = parser.parse_args()

    tracts = gpd.read_parquet(args.tracts)
    hoods = tract_neighborhoods(tracts, gpd.read_parquet(args.neighborhoods))
    combined = pd.read_parquet(args.combined, columns=READ_COLUMNS)
    storm = combined[combined["storm_group"].isin(STORM_GROUPS)]
    located, stats = locate(storm, tracts, hoods)

    last_day = combined["open_dt"].max().normalize() - pd.Timedelta(days=1)
    days = pd.date_range(FIRST_DAY, last_day, freq="D")
    windows = {label: (start, end or last_day) for label, (start, end) in WINDOWS.items()}
    units = {"neighborhood": hoods.dropna().unique(), "geoid20": tracts["geoid20"].unique(), "city": ["Boston"]}

    print("## Location of storm-related requests\n")
    print(markdown(pd.DataFrame([stats])))

    print(f"\n## Candidate units ({days[0].date()} to {last_day.date()})\n")
    rows, group_rows = [], []
    for name, level, by_group, collapse in CANDIDATES:
        counts = panel(located, level, by_group, units[level], days)
        rows += [{"unit": name, **r} for r in unit_metrics(counts, windows, collapse=collapse)]
        if by_group and not collapse:
            group_rows += [{"unit": name, **r} for r in group_metrics(counts, windows)]
    print(markdown(pd.DataFrame(rows)))

    print("\n## By storm group (k 2, m 3)\n")
    print(markdown(pd.DataFrame(group_rows)))


if __name__ == "__main__":
    main()
