"""Feature table per neighborhood and day (#16, ADR-003), keyed (unit, date).

The forecast for day D is made the evening before, at `forecast_cutoff_local`
(Boston time, Data-Pipeline/config/labels_features.csv). Every feature below
uses only what is known by then, and passes ADR-003's history rule (covers 2015
to 2023) and live rule (exists live with the same definition):

- 311: per storm group in `feature_groups`, the count on day D - min_lag and the
  sums over the 3, 7 and 28 days that end there, from daily_counts.parquet
  (#15). Day D-1 is not over at the cutoff, so min_lag is 2. Flooding is left
  out: it has no live source (ADR-003 live rule).
- NWS alerts (ADR-014): per storm group, the alerts whose first bulletin came
  before the cutoff and whose first-announced end is after D begins, and
  whether one of them is a warning (alert_history.py). Alerts exist live
  (#7), so flooding alerts are kept.
- calendar: day of week, month and day of year.
- static attributes per neighborhood (#9; ADR-003: fixed descriptors, vintage
  in docs/data/static-layers.csv): 2025 population, land area and density,
  poverty share, share aged 65 and over and share of households without a car
  (ACS 2019 to 2023), and the share of the neighborhood inside the Climate Ready
  Boston 9-inch sea level rise flood zones (10% and 1% yearly chance).

Weather (observed in training, archived forecasts in validation and test, #7
and #8) and tides (#7) join here once those sources land.

Leakage: each row stores the last 311 day it uses (`features_last_day`) and the
moment the forecast is made (`forecast_cutoff_utc`); the run stops if a row
reaches past D - min_lag or the cutoff is not before D starts.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd

from alert_history import alert_feature_names, alert_features, cutoff_utc, day_start_utc
from daily_counts import STORM_GROUPS
from surge_labels import check_counts, load_settings

WINDOWS = (3, 7, 28)
CALENDAR = ["day_of_week", "month", "day_of_year"]
KEYS = ["unit", "date", "features_last_day", "forecast_cutoff_utc"]
AREA_CRS = "EPSG:26986"  # NAD83 / Massachusetts Mainland, metres
FLOOD_LAYERS = {"flood_10pct": "crb_flood_9in_10pct.parquet", "flood_1pct": "crb_flood_9in_1pct.parquet"}
POPULATION = "population_b01001_001e"
OLDER_ADULTS = [
    "population_b01001_male65_to_69years", "population_b01001_022e", "population_b01001_023e",
    "population_b01001_024e", "population_b01001_025e",
    "population_b01001_female65_to_69years", "population_b01001_046e", "population_b01001_047e",
    "population_b01001_048e", "population_b01001_049e",
]
POVERTY = ("income_b17001_002e", "income_b17001_001e")  # below poverty, people with poverty status
NO_VEHICLE = ("housing_b08201_002e", "housing_b08201_001e")  # households with no vehicle, all households
STATIC_FEATURES = [
    "static_population", "static_land_sqmi", "static_density", "static_poverty_share",
    "static_older_adult_share", "static_no_vehicle_share", "static_flood_10pct_share", "static_flood_1pct_share",
]


def feature_names(groups: list[str]) -> list[str]:
    names = []
    for group in groups:
        names.append(f"{group}_last")
        names += [f"{group}_sum{w}" for w in WINDOWS]
    return names


def lag_features(counts: pd.DataFrame, groups: list[str], min_lag: int) -> pd.DataFrame:
    """Per unit and day, each group's count on day D - min_lag and its sums over WINDOWS ending there.

    Days without enough history (the first days of the table) stay missing,
    never filled: ADR-003 forbids imputed values that never occur live.
    """
    if min_lag < 1:
        raise ValueError("min_lag must be 1 or more, or a feature would use the day it forecasts")
    df = check_counts(counts)
    keys = df[["unit", "date"]].drop_duplicates().sort_values(["unit", "date"], ignore_index=True)
    out = keys.set_index(["unit", "date"])
    for group in groups:
        part = df[df["storm_group"] == group]
        frames = []
        for unit, rows in part.groupby("unit", sort=True):
            series = rows.set_index("date")["requests"].sort_index().astype(float)
            feats = {f"{group}_last": series.shift(min_lag)}
            for w in WINDOWS:
                feats[f"{group}_sum{w}"] = series.rolling(w, min_periods=w).sum().shift(min_lag)
            frames.append(pd.DataFrame(feats).assign(unit=unit).reset_index().set_index(["unit", "date"]))
        if frames:
            out = out.join(pd.concat(frames))
        else:
            out = out.assign(**{name: pd.Series(dtype=float) for name in feature_names([group])})
    out = out.reset_index()
    out.insert(2, "features_last_day", out["date"] - pd.Timedelta(days=min_lag))
    return out


def calendar_features(dates: pd.Series) -> pd.DataFrame:
    """Known for any future day, so they cannot leak."""
    dates = pd.to_datetime(dates)
    return pd.DataFrame({
        "day_of_week": dates.dt.dayofweek.astype("int64"),
        "month": dates.dt.month.astype("int64"),
        "day_of_year": dates.dt.dayofyear.astype("int64"),
    }, index=dates.index)


def _share(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return (numerator / denominator.where(denominator > 0)).round(4)


def static_attributes(static_dir: Path | str) -> pd.DataFrame:
    """One row per neighborhood (the 24 ADR-004 units) with the static features."""
    static_dir = Path(static_dir)
    hoods = gpd.read_parquet(static_dir / "neighborhoods_by_tract20.parquet")[["neighborhood", "geometry"]]
    population = pd.read_parquet(static_dir / "population_2025_neighborhood.parquet")
    needed = {"name", "arealand_sqmi", POPULATION, *OLDER_ADULTS, *POVERTY, *NO_VEHICLE}
    missing = needed - set(population.columns)
    if missing:
        raise ValueError(f"population_2025_neighborhood is missing columns: {sorted(missing)}")
    absent = sorted(set(hoods["neighborhood"]) - set(population["name"]))
    if absent:
        raise ValueError(f"no 2025 population row for neighborhoods {absent}")
    if population["name"].duplicated().any():
        raise ValueError("population_2025_neighborhood repeats a neighborhood name")

    p = population.set_index("name").loc[hoods["neighborhood"]].reset_index(drop=True)
    numbers = p[sorted(needed - {"name"})].apply(pd.to_numeric, errors="coerce")
    out = pd.DataFrame({
        "unit": hoods["neighborhood"].to_numpy(),
        "static_population": numbers[POPULATION].round(0),
        "static_land_sqmi": numbers["arealand_sqmi"].round(4),
        "static_density": _share(numbers[POPULATION], numbers["arealand_sqmi"]).round(1),
        "static_poverty_share": _share(numbers[POVERTY[0]], numbers[POVERTY[1]]),
        "static_older_adult_share": _share(numbers[OLDER_ADULTS].sum(axis=1), numbers[POPULATION]),
        "static_no_vehicle_share": _share(numbers[NO_VEHICLE[0]], numbers[NO_VEHICLE[1]]),
    })
    shapes = hoods.to_crs(AREA_CRS).geometry.make_valid()
    for name, file in FLOOD_LAYERS.items():
        zone = gpd.read_parquet(static_dir / file).to_crs(AREA_CRS).geometry.make_valid().union_all()
        out[f"static_{name}_share"] = _share(shapes.intersection(zone).area.reset_index(drop=True),
                                             shapes.area.reset_index(drop=True))
    return out[["unit", *STATIC_FEATURES]]


def check_no_leakage(features: pd.DataFrame, min_lag: int) -> None:
    """Every row uses 311 days up to D - min_lag and data known before a cutoff that comes before D (ADR-003)."""
    if features.empty:
        return
    gap = (features["date"] - features["features_last_day"]).dt.days
    if (gap < min_lag).any():
        worst = features.loc[gap < min_lag, ["unit", "date", "features_last_day"]].head(3)
        raise AssertionError(f"features reach past day D - {min_lag}:\n{worst.to_string(index=False)}")
    if "forecast_cutoff_utc" in features:
        begins = day_start_utc(pd.DatetimeIndex(features["date"])).as_unit("ns").asi8
        cutoff = pd.DatetimeIndex(features["forecast_cutoff_utc"]).as_unit("ns").asi8
        if (cutoff >= begins).any():
            raise AssertionError("a forecast cutoff is not before the day it forecasts")


def build_features(
    counts: pd.DataFrame,
    groups: list[str],
    min_lag: int,
    alerts: pd.DataFrame | None = None,
    cutoff_local: str = "18:00",
    static: pd.DataFrame | None = None,
    start=None,
    end=None,
) -> pd.DataFrame:
    """The feature table for start to end. Days before start still feed the lags.

    alerts and static are optional so parts can be tested alone; main() always
    passes both, so the table it writes has every feature.
    """
    table = lag_features(counts, groups, min_lag)
    days = pd.DatetimeIndex(table["date"])
    table.insert(3, "forecast_cutoff_utc", cutoff_utc(days, cutoff_local) if len(days) else
                 pd.Series(dtype="datetime64[ns, UTC]"))
    table = pd.concat([table, calendar_features(table["date"])], axis=1)
    columns = [*KEYS, *CALENDAR, *feature_names(groups)]
    if alerts is not None:
        daily = alert_features(alerts, table["date"], cutoff_local, STORM_GROUPS).drop(columns="forecast_cutoff_utc")
        daily["date"] = daily["date"].astype(table["date"].dtype)  # same time unit on both sides of the merge
        table = table.merge(daily, on="date", how="left")
        columns += alert_feature_names(STORM_GROUPS)
    if static is not None:
        absent = sorted(set(table["unit"]) - set(static["unit"]))
        if absent:
            raise ValueError(f"no static attributes for units {absent}")
        table = table.merge(static, on="unit", how="left")
        columns += STATIC_FEATURES
    table = table[columns]
    if start is not None:
        table = table[table["date"] >= pd.Timestamp(start)]
    if end is not None:
        table = table[table["date"] <= pd.Timestamp(end)]
    table = table.sort_values(["unit", "date"], ignore_index=True)
    check_no_leakage(table, min_lag)
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="Data-Pipeline/data/processed/daily_counts.parquet")
    parser.add_argument("--alerts", default="Data-Pipeline/data/interim/nws_alerts.parquet")
    parser.add_argument("--static-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv")
    parser.add_argument("--start", help="first day to write; earlier days still feed the lags")
    parser.add_argument("--end", help="last day to write")
    parser.add_argument("--out", default="Data-Pipeline/data/processed/features.parquet")
    args = parser.parse_args()

    settings = load_settings(args.settings)
    groups, min_lag, cutoff = settings["feature_groups"], settings["feature_min_lag_days"], settings["forecast_cutoff_local"]
    table = build_features(
        pd.read_parquet(args.counts), groups, min_lag,
        alerts=pd.read_parquet(args.alerts), cutoff_local=cutoff,
        static=static_attributes(args.static_dir), start=args.start, end=args.end,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)

    filled = table[feature_names(groups)].notna().all(axis=1)
    print(f"feature groups: {', '.join(groups)}; 311 features use days up to D - {min_lag}; "
          f"alerts known before {cutoff} Boston time the evening before D")
    print(f"{len(table):,} rows, {table.shape[1] - len(KEYS)} features, "
          f"{int(filled.sum()):,} rows with every 311 feature; wrote {out}")
    print("not yet in the table: weather (#7 observed, #8 archived forecasts) and tides (#7)")


if __name__ == "__main__":
    main()
