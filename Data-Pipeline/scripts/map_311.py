"""Map legacy and new-system 311 rows onto one category list and combine them.

Reads the parquets from legacy_311.py and new_311.py, applies
docs/data/category-map.csv, drops legacy copies of cases resubmitted to the
new system, and writes the combined table plus the switch dates.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

UNMAPPED = "unmapped"
MATCHES = {"same", "renamed", "merged", "split", "legacy_only", "new_only", "excluded"}
STORM_GROUPS = {"", "snow_ice", "flooding", "tree"}
MAP_COLUMNS = ["system", "source_category", "category", "storm_group", "match", "note"]

COLUMNS = [
    "case_id",
    "source_system",
    "open_dt",
    "closed_dt",
    "case_status",
    "source_category",
    "category",
    "storm_group",
    "match",
    "department",
    "neighborhood",
    "ward",
    "precinct",
    "zipcode",
    "latitude",
    "longitude",
    "source",
    "after_switch",
]
LEGACY_RENAME = {"case_enquiry_id": "case_id", "type": "source_category", "location_zipcode": "zipcode"}
NEW_RENAME = {
    "open_date": "open_dt",
    "close_date": "closed_dt",
    "service_name": "source_category",
    "assigned_department": "department",
    "zip_code": "zipcode",
    "report_source": "source",
}

# Everything standardize() keeps, plus the resubmitted link. `reason` is not needed.
LEGACY_COLUMNS = [
    "case_enquiry_id",
    "open_dt",
    "closed_dt",
    "case_status",
    "type",
    "department",
    "neighborhood",
    "ward",
    "precinct",
    "location_zipcode",
    "latitude",
    "longitude",
    "source",
    "resubmitted_as",
]

MAX_CLOCK_OFFSET_HOURS = 0.5
MIN_LINKED_CASES = 20


def load_category_map(path: Path | str) -> pd.DataFrame:
    cmap = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(MAP_COLUMNS) - set(cmap.columns)
    if missing:
        raise ValueError(f"category map is missing columns: {sorted(missing)}")

    problems = []
    if not cmap["system"].isin({"legacy", "new"}).all():
        problems.append("system must be legacy or new")
    if cmap.duplicated(["system", "source_category"]).any():
        problems.append("duplicate source categories")
    if (cmap["source_category"] == "").any() or (cmap["category"] == "").any():
        problems.append("blank source_category or category")
    if not cmap["match"].isin(MATCHES).all():
        problems.append(f"match must be one of {sorted(MATCHES)}")
    if not cmap["storm_group"].isin(STORM_GROUPS).all():
        problems.append(f"storm_group must be one of {sorted(STORM_GROUPS - {''})} or blank")
    mapped = cmap[cmap["category"] != UNMAPPED]
    if (mapped.groupby("category")["storm_group"].nunique() > 1).any():
        problems.append("a category has more than one storm_group")
    if problems:
        raise ValueError("bad category map: " + "; ".join(problems))
    return cmap


def standardize(df: pd.DataFrame, system: str) -> pd.DataFrame:
    rename = LEGACY_RENAME if system == "legacy" else NEW_RENAME
    out = df.rename(columns=rename).assign(source_system=system)
    return out.reindex(columns=[c for c in COLUMNS if c not in {"category", "storm_group", "match", "after_switch"}])


def apply_map(df: pd.DataFrame, cmap: pd.DataFrame, system: str) -> pd.DataFrame:
    # A lookup, not a merge: a merge copies every column of the 3M-row legacy table.
    side = cmap.loc[cmap["system"] == system].set_index("source_category")
    source = df["source_category"]
    out = df.assign(
        category=source.map(side["category"]).fillna(UNMAPPED),
        storm_group=source.map(side["storm_group"]).replace("", pd.NA),
        match=source.map(side["match"]).fillna(UNMAPPED),
    )
    return out.reindex(columns=COLUMNS)


def drop_resubmitted(legacy: pd.DataFrame, new_ids: pd.Series) -> tuple[pd.DataFrame, int]:
    copied = legacy["resubmitted_as"].isin(set(new_ids))
    return legacy[~copied], int(copied.sum())


def clock_offset_hours(legacy: pd.DataFrame, new: pd.DataFrame) -> tuple[float, int]:
    """Median of new open time minus legacy close time over resubmitted cases.

    The new case is opened when the legacy one is closed, so this is close to
    zero if both clocks are Boston local and 4 to 5 hours if the new one is UTC.
    """
    linked = legacy.dropna(subset=["resubmitted_as", "closed_dt"]).merge(
        new[["case_id", "open_date"]], left_on="resubmitted_as", right_on="case_id"
    )
    if linked.empty:
        return float("nan"), 0
    diff = (linked["open_date"] - linked["closed_dt"]).dt.total_seconds() / 3600
    return float(diff.median()), len(linked)


def check_clock(legacy: pd.DataFrame, new: pd.DataFrame) -> tuple[float, int]:
    offset, n = clock_offset_hours(legacy, new)
    if n >= MIN_LINKED_CASES and abs(offset) > MAX_CLOCK_OFFSET_HOURS:
        raise ValueError(
            f"new-system clock is off by {offset:.2f}h from legacy over {n} resubmitted cases; "
            "check new_311.CLOCK"
        )
    return offset, n


def change_points(combined: pd.DataFrame) -> pd.DataFrame:
    """First new-system date for every category and storm group fed by both systems."""
    columns = ["level", "name", "storm_group", "switch_date", "legacy_last", "legacy_after_switch", "note"]
    if combined.empty:
        return pd.DataFrame(columns=columns)
    opened = combined["open_dt"]
    is_legacy = combined["source_system"] == "legacy"
    new_start = opened[~is_legacy].min()
    rows = []
    for level in ("category", "storm_group"):
        key = combined[level]
        new_first = opened.where(~is_legacy).groupby(key).min().dt.normalize()
        legacy_last = opened.where(is_legacy).groupby(key).max()
        both = new_first.notna() & legacy_last.reindex(new_first.index).notna()
        names = sorted(n for n in new_first.index[both] if n != UNMAPPED)
        if not names:
            continue
        switch = new_first[names]
        after = (is_legacy & (opened >= key.map(switch))).groupby(key).sum()
        group = combined["storm_group"].groupby(key).first()
        for name in names:
            rows.append({
                "level": level,
                "name": name,
                "storm_group": group.get(name) if pd.notna(group.get(name)) else "",
                "switch_date": switch[name].date(),
                "legacy_last": legacy_last[name].date(),
                "legacy_after_switch": int(after[name]),
                "note": "new data starts here, switch may be earlier" if switch[name] == new_start.normalize() else "",
            })
    return pd.DataFrame(rows, columns=columns)


def flag_after_switch(combined: pd.DataFrame, points: pd.DataFrame) -> pd.Series:
    """Legacy rows opened on or after their category's first new-system day.

    Only flags them; what to do with them is #53.
    """
    if combined.empty:
        return pd.Series(False, index=combined.index)
    switch = points.loc[points["level"] == "category"].set_index("name")["switch_date"]
    starts = pd.to_datetime(combined["category"].map(switch))
    return (combined["source_system"] == "legacy") & (combined["open_dt"] >= starts)


def combine(legacy: pd.DataFrame, new: pd.DataFrame, cmap: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    offset, linked = check_clock(legacy, new)
    legacy, resubmitted = drop_resubmitted(legacy, new["case_id"])
    parts = [
        apply_map(standardize(legacy, "legacy"), cmap, "legacy"),
        apply_map(standardize(new, "new"), cmap, "new"),
    ]
    parts = [p for p in parts if not p.empty]
    combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    combined = combined.reindex(columns=COLUMNS)
    combined["after_switch"] = flag_after_switch(combined, change_points(combined)).astype(bool)
    return combined, {
        "clock_offset_hours": offset,
        "linked_cases": linked,
        "resubmitted_dropped": resubmitted,
        "after_switch": int(combined["after_switch"].sum()),
    }


def summary(combined: pd.DataFrame) -> pd.DataFrame:
    counts = combined.groupby(["source_system", "match"]).size().unstack(fill_value=0)
    counts["total"] = counts.sum(axis=1)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", default="Data-Pipeline/data/interim/legacy_311.parquet")
    parser.add_argument("--new", default="Data-Pipeline/data/interim/new_311.parquet")
    parser.add_argument("--map", default="docs/data/category-map.csv")
    parser.add_argument("--out", default="Data-Pipeline/data/interim/311_combined.parquet")
    parser.add_argument("--change-points", default="docs/data/change-points.csv")
    args = parser.parse_args()

    cmap = load_category_map(args.map)
    # Read inline so main() holds no reference to the unfiltered legacy table
    # while combine() works; that copy alone was about 0.6 GB.
    combined, stats = combine(
        pd.read_parquet(args.legacy, columns=LEGACY_COLUMNS), pd.read_parquet(args.new), cmap
    )
    print(f"clock check: median offset {stats['clock_offset_hours']:+.2f}h over {stats['linked_cases']} resubmitted cases")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(out, index=False)
    change_points(combined).to_csv(args.change_points, index=False)

    print(f"dropped {stats['resubmitted_dropped']:,} legacy rows resubmitted to the new system")
    print(f"flagged {stats['after_switch']:,} legacy rows opened after their category switched (after_switch)")
    print(summary(combined).to_string())
    unmapped = combined.loc[combined["match"] == UNMAPPED, ["source_system", "source_category"]]
    if not unmapped.empty:
        print("unmapped source categories:")
        print(unmapped.value_counts(dropna=False).to_string())
    print(f"wrote {len(combined):,} rows to {out} and switch dates to {args.change_points}")


if __name__ == "__main__":
    main()
