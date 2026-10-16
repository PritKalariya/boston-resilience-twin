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
    return out.reindex(columns=[c for c in COLUMNS if c not in {"category", "storm_group", "match"}])


def apply_map(df: pd.DataFrame, cmap: pd.DataFrame, system: str) -> pd.DataFrame:
    side = cmap.loc[cmap["system"] == system, ["source_category", "category", "storm_group", "match"]]
    out = df.merge(side, on="source_category", how="left", validate="many_to_one")
    unknown = out["category"].isna()
    out.loc[unknown, "category"] = UNMAPPED
    out.loc[unknown, "match"] = UNMAPPED
    out["storm_group"] = out["storm_group"].fillna("").replace("", pd.NA)
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
    new_start = combined.loc[combined["source_system"] == "new", "open_dt"].min()
    rows = []
    levels = [("category", "category"), ("storm_group", "storm_group")]
    for level, key in levels:
        for name, g in combined.dropna(subset=[key]).groupby(key):
            if name == UNMAPPED:
                continue
            legacy = g.loc[g["source_system"] == "legacy", "open_dt"]
            new = g.loc[g["source_system"] == "new", "open_dt"]
            if legacy.empty or new.empty:
                continue
            switch = new.min().normalize()
            rows.append({
                "level": level,
                "name": name,
                "storm_group": g["storm_group"].dropna().iloc[0] if g["storm_group"].notna().any() else "",
                "switch_date": switch.date(),
                "legacy_last": legacy.max().date(),
                "legacy_after_switch": int((legacy >= switch).sum()),
                "note": "new data starts here, switch may be earlier" if switch == new_start.normalize() else "",
            })
    return pd.DataFrame(rows, columns=[
        "level", "name", "storm_group", "switch_date", "legacy_last", "legacy_after_switch", "note",
    ])


def combine(legacy: pd.DataFrame, new: pd.DataFrame, cmap: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    legacy, resubmitted = drop_resubmitted(legacy, new["case_id"])
    parts = [
        apply_map(standardize(legacy, "legacy"), cmap, "legacy"),
        apply_map(standardize(new, "new"), cmap, "new"),
    ]
    parts = [p for p in parts if not p.empty]
    combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return combined.reindex(columns=COLUMNS), {"resubmitted_dropped": resubmitted}


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
    legacy = pd.read_parquet(args.legacy)
    new = pd.read_parquet(args.new)

    offset, linked = check_clock(legacy, new)
    print(f"clock check: median offset {offset:+.2f}h over {linked} resubmitted cases")

    combined, stats = combine(legacy, new, cmap)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(out, index=False)
    change_points(combined).to_csv(args.change_points, index=False)

    print(f"dropped {stats['resubmitted_dropped']:,} legacy rows resubmitted to the new system")
    print(summary(combined).to_string())
    unmapped = combined.loc[combined["match"] == UNMAPPED, ["source_system", "source_category"]]
    if not unmapped.empty:
        print("unmapped source categories:")
        print(unmapped.value_counts(dropna=False).to_string())
    print(f"wrote {len(combined):,} rows to {out} and switch dates to {args.change_points}")


if __name__ == "__main__":
    main()
