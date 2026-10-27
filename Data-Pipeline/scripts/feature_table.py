"""Feature table per neighborhood and day (#16, ADR-003), keyed (unit, date).

The forecast for day D is made the evening before, at `forecast_cutoff_local`
(Boston time, Data-Pipeline/config/labels_features.csv). Every feature below
uses only what is known by then, and passes ADR-003's history rule (covers 2015
to 2023) and live rule (exists live with the same definition), except where
Data-Pipeline/config/feature_catalog.csv says otherwise and keeps the column
out of the model:

- 311: per storm group in `feature_groups`, the count on day D - min_lag and the
  sums over the 3, 7 and 28 days that end there, from daily_counts.parquet
  (#15). Day D-1 is not over at the cutoff, so min_lag is 2. Flooding is left
  out: it has no live source (ADR-003 live rule).
- NWS alerts (ADR-014, Option A+): per storm group, the alerts whose first
  bulletin came before the cutoff, whose first-announced end is after D begins
  and that were not cancelled or upgraded before the cutoff, and whether one of
  them is a warning (alert_history.py). One VTEC event counts once. Alerts
  exist live (the evening IEM pull, checked against #7's NWS snapshot), so
  flooding alerts are kept.
- calendar: day of week, month and day of year.
- static attributes per neighborhood (#9; ADR-003: fixed descriptors, vintage
  in docs/data/static-layers.csv): 2025 population, land area and density,
  poverty share, share aged 65 and over and share of households without a car
  (ACS 2019 to 2023), and the share of the neighborhood inside the Climate Ready
  Boston 9-inch sea level rise flood zones (10% and 1% yearly chance).
- weather (weather_features.py, the same for every unit): Boston Logan's
  observed GHCN-D day D in the training years and, from 2024, the GFS
  day-before forecasts known at the cutoff (ADR-003; #7 and #8), with
  `wx_source`; and Logan's observed day D - observed_weather_lag_days, kept out
  of the model until it passes the live rule.
- tides (tide_features.py, the same for every unit): the highest predicted
  water level at the Boston gauge on day D (#7).

Leakage: each row stores the last 311 day it uses (`features_last_day`) and the
moment the forecast is made (`forecast_cutoff_utc`); the run stops if a row
reaches past D - min_lag, a 311 feature is not what the counts give when read by
date up to features_last_day (lags_by_date), the cutoff is not before D starts,
a row after the training years takes observed weather of its own day, or a
forecast it uses became available only after the cutoff (`wx_known_by_utc`).
It also stops before any of this when a day from --start to --end is not in the
counts (surge_labels.requested_days).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from alert_history import alert_feature_names, alert_features, cutoff_utc, day_start_utc
from daily_counts import STORM_GROUPS
from surge_labels import check_counts, load_settings, requested_days
from tide_features import TIDE_FEATURES, tide_features
from weather_features import OBSERVED_UNTIL, WEATHER_FEATURES, weather_features

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
        # min_count: one missing age group leaves the share missing instead of too low
        "static_older_adult_share": _share(numbers[OLDER_ADULTS].sum(axis=1, min_count=len(OLDER_ADULTS)),
                                           numbers[POPULATION]),
        "static_no_vehicle_share": _share(numbers[NO_VEHICLE[0]], numbers[NO_VEHICLE[1]]),
    })
    shapes = hoods.to_crs(AREA_CRS).geometry.make_valid()
    for name, file in FLOOD_LAYERS.items():
        zone = gpd.read_parquet(static_dir / file).to_crs(AREA_CRS).geometry.make_valid().union_all()
        out[f"static_{name}_share"] = _share(shapes.intersection(zone).area.reset_index(drop=True),
                                             shapes.area.reset_index(drop=True))
    return out[["unit", *STATIC_FEATURES]]


def lags_by_date(counts: pd.DataFrame, rows: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """The 311 features of each row recomputed by date: each group's count on the row's
    features_last_day and its sums over WINDOWS ending there, missing where a day they need is not
    in the counts. lag_features shifts rows; this looks the days up, so the two agree only if every
    feature reads exactly the days up to features_last_day."""
    df = check_counts(counts)
    first = df["date"].min()
    calendar = pd.date_range(first, df["date"].max())
    units = pd.Index(sorted(df["unit"].unique()))
    column = units.get_indexer(rows["unit"])
    position = ((pd.DatetimeIndex(rows["features_last_day"]) - first) // pd.Timedelta(days=1)).to_numpy()
    known = (column >= 0) & (position >= 0) & (position < len(calendar))
    col, pos = np.where(known, column, 0), np.where(known, position, 0)
    out = pd.DataFrame(index=rows.index)
    for group in groups:
        grid = (df[df["storm_group"] == group].pivot(index="date", columns="unit", values="requests")
                .reindex(index=calendar, columns=units).to_numpy(dtype="float64"))
        before = np.vstack([np.zeros((1, len(units))), grid.cumsum(axis=0)])  # before[i]: days 0 to i - 1
        out[f"{group}_last"] = np.where(known, grid[pos, col], np.nan)
        for w in WINDOWS:
            whole = known & (position >= w - 1)
            out[f"{group}_sum{w}"] = np.where(whole, before[pos + 1, col] - before[np.maximum(pos + 1 - w, 0), col], np.nan)
    return out


def check_no_leakage(features: pd.DataFrame, min_lag: int, counts: pd.DataFrame | None = None,
                     groups: list[str] = ()) -> None:
    """Every row uses 311 days up to D - min_lag and data known before a cutoff that comes before D (ADR-003).

    With the counts, each 311 feature must also equal lags_by_date, its value read by date up to
    features_last_day. features_last_day is date - min_lag by construction, so the gap check alone
    cannot see a lag that reads a later day than it says; this comparison can.
    """
    if features.empty:
        return
    gap = (features["date"] - features["features_last_day"]).dt.days
    if (gap < min_lag).any():
        worst = features.loc[gap < min_lag, ["unit", "date", "features_last_day"]].head(3)
        raise AssertionError(f"features reach past day D - {min_lag}:\n{worst.to_string(index=False)}")
    if counts is not None:
        expected = lags_by_date(counts, features, list(groups))
        for name in feature_names(list(groups)):
            got, want = features[name].to_numpy(dtype="float64"), expected[name].to_numpy()
            wrong = ~((got == want) | (np.isnan(got) & np.isnan(want)))
            if wrong.any():
                worst = features.loc[wrong, ["unit", "date", "features_last_day", name]].assign(by_date=want[wrong])
                raise AssertionError(f"{name} is not the counts up to features_last_day, read by date:\n"
                                     f"{worst.head(3).to_string(index=False)}")
    if "forecast_cutoff_utc" in features:
        begins = day_start_utc(pd.DatetimeIndex(features["date"])).as_unit("ns").asi8
        cutoff = pd.DatetimeIndex(features["forecast_cutoff_utc"]).as_unit("ns").asi8
        if (cutoff >= begins).any():
            raise AssertionError("a forecast cutoff is not before the day it forecasts")
    if "wx_source" in features:
        own_day = (features["wx_source"] == "observed") & (features["date"] > OBSERVED_UNTIL)
        if own_day.any():
            raise AssertionError(f"rows after {OBSERVED_UNTIL.date()} take observed weather of their own day (ADR-003)")
    if "wx_known_by_utc" in features:
        known = pd.DatetimeIndex(features["wx_known_by_utc"]).as_unit("ns").asi8  # missing: the smallest number
        cutoff = pd.DatetimeIndex(features["forecast_cutoff_utc"]).as_unit("ns").asi8
        if (known > cutoff).any():
            raise AssertionError("a row uses a forecast that became available only after its cutoff")


def written_days(counts: pd.DataFrame, start=None, end=None) -> pd.DatetimeIndex:
    """The days the table writes: every day from start to end, each of them in the counts
    (requested_days stops the run when one is not)."""
    return requested_days(counts, start, end)


def build_features(
    counts: pd.DataFrame,
    groups: list[str],
    min_lag: int,
    alerts: pd.DataFrame | None = None,
    cutoff_local: str = "18:00",
    static: pd.DataFrame | None = None,
    start=None,
    end=None,
    weather: pd.DataFrame | None = None,
    tides: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The feature table for start to end. Days before start still feed the lags.

    alerts, static, weather and tides are optional so parts can be tested
    alone; main() passes every group it has inputs for. weather and tides have
    one row per day (weather_features, tide_features) and must cover every day
    the table writes.
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
    joined = {}
    for name, daily in (("weather", weather), ("tides", tides)):
        if daily is None:
            continue
        if daily["date"].duplicated().any():
            raise ValueError(f"{name} has two rows for a day")
        daily = daily.assign(date=pd.to_datetime(daily["date"]).astype(table["date"].dtype))
        joined[name] = f"_has_{name}"
        table = table.merge(daily.assign(**{joined[name]: True}), on="date", how="left")
        columns += [column for column in daily.columns if column != "date"]
    table = table[columns + list(joined.values())]
    if start is not None:
        table = table[table["date"] >= pd.Timestamp(start)]
    if end is not None:
        table = table[table["date"] <= pd.Timestamp(end)]
    for name, flag in joined.items():
        absent = table.loc[table[flag].isna(), "date"]
        if len(absent):
            raise ValueError(f"{name} has no row for {absent.nunique()} of the days written, from "
                             f"{absent.min().date()}; build it for every day")
    table = table.drop(columns=list(joined.values())).sort_values(["unit", "date"], ignore_index=True)
    check_no_leakage(table, min_lag, counts, groups)
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="Data-Pipeline/data/processed/daily_counts.parquet")
    parser.add_argument("--alerts", default="Data-Pipeline/data/interim/nws_alerts.parquet")
    parser.add_argument("--static-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--noaa-dir", help="#7's GHCN-D folder (raw/weather/noaa_logan); with --forecast-dir, "
                        "adds the weather features")
    parser.add_argument("--forecast-dir", help="#8's GFS day-before folder (raw/open_meteo/day_before_ncep_gfs_global)")
    parser.add_argument("--tides-dir", help="#7's tide predictions folder (raw/tides/predicted); adds the tide features")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv")
    parser.add_argument("--start", help="first day to write; earlier days still feed the lags")
    parser.add_argument("--end", help="last day to write")
    parser.add_argument("--out", default="Data-Pipeline/data/processed/features.parquet")
    args = parser.parse_args()
    if bool(args.noaa_dir) != bool(args.forecast_dir):
        parser.error("the weather features need both --noaa-dir (#7) and --forecast-dir (#8)")

    settings = load_settings(args.settings)
    groups, min_lag, cutoff = settings["feature_groups"], settings["feature_min_lag_days"], settings["forecast_cutoff_local"]
    counts = pd.read_parquet(args.counts)
    days = written_days(counts, args.start, args.end)
    weather = tides = None
    if args.noaa_dir:
        weather = weather_features(days, cutoff, args.noaa_dir, args.forecast_dir, settings["observed_weather_lag_days"],
                                   settings["forecast_issue_margin_hours"])
    if args.tides_dir:
        tides = tide_features(days, args.tides_dir)
    table = build_features(
        counts, groups, min_lag,
        alerts=pd.read_parquet(args.alerts), cutoff_local=cutoff,
        static=static_attributes(args.static_dir), start=args.start, end=args.end,
        weather=weather, tides=tides,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)

    filled = table[feature_names(groups)].notna().all(axis=1)
    print(f"feature groups: {', '.join(groups)}; 311 features use days up to D - {min_lag}; "
          f"alerts known before {cutoff} Boston time the evening before D")
    print(f"{len(table):,} rows, {table.shape[1] - len(KEYS)} columns besides the keys, "
          f"{int(filled.sum()):,} rows with every 311 feature; wrote {out}")
    missing = []
    if weather is None:
        missing.append("weather (--noaa-dir from #7 and --forecast-dir from #8)")
    else:
        forecast = weather[weather["wx_source"] == "forecast"]
        gaps = ", ".join(f"{name} {int(forecast[name].isna().sum())}" for name in WEATHER_FEATURES)
        print(f"weather: {int((weather['wx_source'] == 'observed').sum()):,} days observed (to {OBSERVED_UNTIL.date()}), "
              f"{len(forecast):,} days forecast; forecast days missing, kept missing: {gaps}")
    if tides is None:
        missing.append("tides (--tides-dir from #7)")
    else:
        print(f"tides: {int(tides[TIDE_FEATURES[0]].isna().sum()):,} days without every hour of predictions, kept missing")
    if missing:
        print(f"not yet in the table: {'; '.join(missing)}")


if __name__ == "__main__":
    main()
