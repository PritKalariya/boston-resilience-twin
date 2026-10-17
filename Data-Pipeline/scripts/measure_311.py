"""Re-measure legacy 311 counts, cell sparsity, and storm ratios.

Prints markdown tables for docs/status.md. Uses the parquet written by
legacy_311.py and the Logan GHCN-D daily file.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from legacy_311 import count_records

NEIGHBORHOOD_ALIASES = {
    "South Boston": "South Boston / South Boston Waterfront",
    "Allston": "Allston / Brighton",
    "Brighton": "Allston / Brighton",
    "Chestnut Hill": "Allston / Brighton",
    "Mattapan": "Greater Mattapan",
}
NO_NEIGHBORHOOD = {"Boston", ""}

SNOW_DAY_MM = 25.0
HEAVY_RAIN_MM = 25.4
DRY_DAY_MM = 2.5
HEAT_DAY_C = 32.2
AFTERMATH_DAYS = 3


def legacy_storm_groups(path: Path | str) -> pd.DataFrame:
    cmap = pd.read_csv(path, dtype=str, keep_default_na=False)
    legacy = cmap[(cmap["system"] == "legacy") & (cmap["storm_group"] != "")]
    return legacy.rename(columns={"source_category": "type"})[["type", "storm_group"]]


def normalize_neighborhood(values: pd.Series) -> pd.Series:
    cleaned = values.astype("string").str.strip()
    cleaned = cleaned.replace(NEIGHBORHOOD_ALIASES)
    return cleaned.mask(cleaned.isin(NO_NEIGHBORHOOD))


def load_weather(path: Path | str) -> pd.DataFrame:
    raw = pd.read_csv(path, usecols=["DATE", "PRCP", "SNOW", "TMAX"], dtype=str)
    out = pd.DataFrame({"date": pd.to_datetime(raw["DATE"], format="%Y-%m-%d")})
    out["prcp_mm"] = pd.to_numeric(raw["PRCP"].str.strip(), errors="coerce") / 10
    out["snow_mm"] = pd.to_numeric(raw["SNOW"].str.strip(), errors="coerce")
    out["tmax_c"] = pd.to_numeric(raw["TMAX"].str.strip(), errors="coerce") / 10
    return out.set_index("date")


def inferred_format_loss(path: Path | str) -> int:
    """Rows lost when pandas infers the open_dt format, as the first run likely did."""
    open_dt = pd.read_csv(path, usecols=["open_dt"], dtype=str)["open_dt"]
    return int(pd.to_datetime(open_dt, errors="coerce").isna().sum())


def reconcile(raw_dir: Path, parsed: pd.DataFrame, published: pd.DataFrame) -> pd.DataFrame:
    parsed_by_year = parsed["open_dt"].dt.year.value_counts()
    rows = []
    for source, published_rows in published.set_index("source")["published_rows"].items():
        path = raw_dir / source
        row = {"source": source, "published": published_rows, "csv_records": count_records(path)}
        if source[:4].isdigit():
            row["inferred_format_lost"] = inferred_format_loss(path)
            row["parsed"] = int(parsed_by_year.get(int(source[:4]), 0))
        rows.append(row)

    out = pd.DataFrame(rows).astype({"inferred_format_lost": "Int64", "parsed": "Int64"})
    out["inferred_format_lost_pct"] = (100 * out["inferred_format_lost"] / out["published"]).round(1)
    out["now_lost"] = out["published"] - out["parsed"]
    return out


def daily_cells(df: pd.DataFrame, storm_groups: pd.DataFrame) -> pd.DataFrame:
    storm = df.merge(storm_groups, on="type", how="inner")
    storm = storm.assign(
        neighborhood=normalize_neighborhood(storm["neighborhood"]),
        date=storm["open_dt"].dt.normalize(),
    )
    return storm.dropna(subset=["neighborhood"])


def sparsity(cells: pd.DataFrame, start, end, by_group: bool) -> dict:
    days = pd.date_range(start, end, freq="D")
    neighborhoods = cells["neighborhood"].unique()
    keys = ["neighborhood", "date"] + (["storm_group"] if by_group else [])
    window = cells[cells["date"].between(days[0], days[-1])]
    filled = window.groupby(keys).ngroups

    total = len(days) * len(neighborhoods)
    if by_group:
        total *= cells["storm_group"].nunique()
    return {"cells": total, "non_empty": filled, "empty_pct": round(100 * (1 - filled / total), 1)}


def matched_ratio(daily: pd.Series, event: pd.Series, control: pd.Series, lag: int = 0) -> float:
    """Count `lag` days after each event day over the mean of control days in the same month.

    Control days within AFTERMATH_DAYS of an event are skipped, since cleanup
    requests keep coming in after a storm and would inflate the baseline.
    """
    recent = event.astype(int).rolling(AFTERMATH_DAYS + 1, min_periods=1).max().astype(bool)
    if daily.empty:
        return float("nan")
    frame = pd.DataFrame({"n": daily, "after": daily.shift(-lag), "event": event, "control": control & ~recent})
    frame["month"] = frame.index.to_period("M")
    baseline = frame[frame["control"]].groupby("month")["n"].mean()

    events = frame[frame["event"]].join(baseline.rename("expected"), on="month")
    events = events.dropna(subset=["after", "expected"])
    if events.empty or events["expected"].sum() == 0:
        return float("nan")
    return round(events["after"].sum() / events["expected"].sum(), 2)


def storm_ratios(df: pd.DataFrame, storm_groups: pd.DataFrame, weather: pd.DataFrame, start, end) -> pd.DataFrame:
    days = pd.date_range(start, end, freq="D")
    window = df[df["open_dt"].between(days[0], days[-1] + pd.Timedelta(days=1), inclusive="left")]
    dates = window["open_dt"].dt.normalize()
    groups = window["type"].map(storm_groups.set_index("type")["storm_group"])

    def daily(mask=None) -> pd.Series:
        picked = dates if mask is None else dates[mask]
        return picked.value_counts().reindex(days, fill_value=0)

    w = weather.reindex(days)
    snow, rain, heat = w["snow_mm"], w["prcp_mm"], w["tmax_c"]
    summer = pd.Series(days.month.isin([6, 7, 8, 9]), index=days)
    checks = [
        ("snow_ice on snow days", daily(groups == "snow_ice"), snow >= SNOW_DAY_MM, snow == 0, "4.9x"),
        ("flooding on heavy rain days", daily(groups == "flooding"), rain >= HEAVY_RAIN_MM, rain < DRY_DAY_MM, "2.5x"),
        ("tree on heavy rain days", daily(groups == "tree"), rain >= HEAVY_RAIN_MM, rain < DRY_DAY_MM, "1.7x"),
        ("all requests on heat days", daily(), heat >= HEAT_DAY_C, (heat < HEAT_DAY_C) & summer, "1.13x"),
    ]
    rows = []
    for name, counts, event, control, first_run in checks:
        event, control = event.fillna(False).astype(bool), control.fillna(False).astype(bool)
        rows.append({
            "check": name,
            "event_days": int(event.sum()),
            "same_day": matched_ratio(counts, event, control),
            "next_day": matched_ratio(counts, event, control, lag=1),
            "first_run": first_run,
        })
    return pd.DataFrame(rows)


def markdown(df: pd.DataFrame) -> str:
    cells = df.astype(object).where(df.notna(), "").astype(str)
    lines = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(row) + " |" for row in cells.itertuples(index=False)]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parsed", default="Data-Pipeline/data/interim/legacy_311.parquet")
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/311")
    parser.add_argument("--weather", default="Data-Pipeline/data/raw/weather/ghcnd_USW00014739.csv")
    parser.add_argument("--published", default="Data-Pipeline/config/published_311_counts.csv")
    parser.add_argument("--map", default="docs/data/category-map.csv")
    args = parser.parse_args()

    df = pd.read_parquet(args.parsed, columns=["open_dt", "type", "neighborhood"])
    storm_groups = legacy_storm_groups(args.map)
    weather = load_weather(args.weather)
    published = pd.read_csv(args.published)

    print("## Reconciliation\n")
    print(markdown(reconcile(Path(args.raw_dir), df, published)))

    cells = daily_cells(df, storm_groups)
    last_day = df["open_dt"].max().normalize() - pd.Timedelta(days=1)
    windows = {"2015 to 2026": ("2015-01-01", last_day), "2024 to 2026": ("2024-01-01", last_day)}

    print("\n## Sparsity of storm-related cells\n")
    rows = []
    for label, (start, end) in windows.items():
        for by_group in (False, True):
            unit = "neighborhood x day x group" if by_group else "neighborhood x day"
            rows.append({"window": label, "unit": unit, **sparsity(cells, start, end, by_group)})
    print(markdown(pd.DataFrame(rows)))

    print("\n## Storm ratios\n")
    for label, (start, end) in windows.items():
        print(f"\n{label}\n")
        print(markdown(storm_ratios(df, storm_groups, weather, start, end)))


if __name__ == "__main__":
    main()
