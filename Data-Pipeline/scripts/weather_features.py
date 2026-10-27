"""Weather features per day (#16, ADR-003): observed in training, archived forecasts after.

ADR-003 as written: the training rows (days up to OBSERVED_UNTIL, the end of
the 2015 to 2023 training years) take Boston Logan's observed GHCN-D values
for day D itself (#7), so backtests on them are upper bounds. Every later row
(validation 2024, test 2025 onward, and the evening run) takes the archived GFS
day-before forecasts for the same values, as known at the cutoff (#8,
open_meteo_forecast.py). A row after OBSERVED_UNTIL never takes an observation
of its own day: wx_source says which source a row has, and wx_known_by_utc when
the newest forecast it uses became available. Both sources describe Logan's
station day, midnight to midnight Eastern standard time.

| feature        | observed (GHCN-D, #7)        | forecast (GFS, #8)        |
|----------------|------------------------------|---------------------------|
| wx_precip_mm   | PRCP                         | precipitation, summed     |
| wx_snowfall_mm | SNOW                         | snowfall, cm x 10, summed |
| wx_tmax_c      | TMAX                         | temperature_2m, highest   |
| wx_tmin_c      | TMIN                         | temperature_2m, lowest    |
| wx_gust_max_ms | WSF5 (fastest 5-second wind) | wind_gusts_10m, highest   |

Lagged observations, obs_<value>_lag<k> with k = observed_weather_lag_days
(2): GHCN-D on day D - k, for every row. Logan's day D-2 ends before the
cutoff, and they cover the training years, but CDO ran about 5 days behind on
2026-10-25 (#7 data card), so at k = 2 the evening run cannot fill them.
feature_catalog.csv keeps them out of the model until a live source or a
longer lag passes ADR-003's live rule. Snow depth (SNWD) is left out: Logan
reports none from 2015.

Missing stays missing: a GHCN-D value with a quality flag counts as missing,
and no gap is filled, with an observation or anything else. Every year file
from FIRST_YEAR to last year must be stored; only the current year may be
absent (not downloaded yet), and its days stay missing, with a warning that
names the file (period_files.py).

#7 stores Data-Pipeline/data/raw/weather/noaa_logan/<year>.json exactly as CDO
returned it: {"metadata": ..., "results": [{"date", "datatype", "station",
"attributes", "value"}]}, in metric units (#7 asks for units=metric; the file
names no unit). Checked against NOAA_CHECKED: test_feature_sources.py reads
what noaa_logan.py itself stores.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import open_meteo_forecast as om
from alert_history import cutoff_utc
from period_files import stored_periods

NOAA_CHECKED = "#7 as merged (noaa_logan.py, main df56bd4)"
STATION = "GHCND:USW00014739"  # Boston Logan (#7)
FIRST_YEAR = 2015  # #7 downloads Logan from 2015-01-01
OBSERVED_UNTIL = pd.Timestamp("2023-12-31")  # ADR-003: the last training day; later rows take forecasts
# GHCN-D datatype -> observed value
OBSERVED = {"PRCP": "prcp_mm", "SNOW": "snow_mm", "TMAX": "tmax_c", "TMIN": "tmin_c", "AWND": "awnd_ms",
            "WSF5": "wsf5_ms"}
# Weather feature -> (observed value, forecast value): the ADR-003 pairs
PAIRS = {
    "wx_precip_mm": ("prcp_mm", "precip_mm"),
    "wx_snowfall_mm": ("snow_mm", "snowfall_mm"),
    "wx_tmax_c": ("tmax_c", "tmax_c"),
    "wx_tmin_c": ("tmin_c", "tmin_c"),
    "wx_gust_max_ms": ("wsf5_ms", "gust_max_ms"),
}
WEATHER_META = ["wx_source", "wx_known_by_utc"]
WEATHER_FEATURES = list(PAIRS)


def lag_names(lag_days: int) -> list[str]:
    return [f"obs_{name}_lag{lag_days}" for name in OBSERVED.values()]


def weather_columns(lag_days: int) -> list[str]:
    return [*WEATHER_META, *WEATHER_FEATURES, *lag_names(lag_days)]


def _read_noaa_year(path: Path) -> pd.DataFrame:
    """One stored CDO answer as (date, name, value) rows; raises when it does not look like #7's file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path} is not JSON ({exc})") from exc
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError(f"{path} has no results list; recheck against #7 ({NOAA_CHECKED})")
    rows = pd.DataFrame(results, columns=["date", "datatype", "station", "attributes", "value"])
    others = set(rows["station"].dropna()) - {STATION}
    if others:
        raise ValueError(f"{path} holds stations {sorted(others)}; expected only {STATION}")
    rows = rows[rows["datatype"].isin(list(OBSERVED))]
    try:
        value = pd.to_numeric(rows["value"], errors="raise").astype("float64")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} has a value that is not a number") from exc
    # attributes: measurement flag, quality flag, source flag, time; a quality flag marks a failed check
    quality = rows["attributes"].fillna("").astype(str).str.split(",").str[1].fillna("").str.strip()
    out = pd.DataFrame({"date": pd.to_datetime(rows["date"].astype(str).str[:10], format="%Y-%m-%d"),
                        "name": rows["datatype"].map(OBSERVED), "value": value.where(quality == "")})
    if out.duplicated(["date", "name"]).any():
        raise ValueError(f"{path} repeats a day and datatype")
    return out


def read_observed(noaa_dir, years, today: date | None = None) -> pd.DataFrame:
    """Logan's GHCN-D values per station day (index date, one column per OBSERVED value) for the given years.

    Years before FIRST_YEAR are not asked for. Every other year before the
    current one must be stored; the current year may be absent, with a warning
    (period_files).
    """
    noaa_dir = Path(noaa_dir)
    this_year = (today or datetime.now(timezone.utc).date()).year
    years = sorted(year for year in set(years) if year >= FIRST_YEAR)
    stored = stored_periods(noaa_dir, years, this_year, "run noaa_logan.py (#7)",
                            "Logan's observed values (obs_*_lag*, and wx_* up to 2023-12-31)")
    frames = [_read_noaa_year(noaa_dir / f"{year}.json") for year in stored]
    columns = list(OBSERVED.values())
    if not frames:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], name="date"), dtype="float64")
    long = pd.concat(frames, ignore_index=True)
    if long.duplicated(["date", "name"]).any():
        raise ValueError(f"{noaa_dir}: a day is stored in two files")
    return long.pivot(index="date", columns="name", values="value").reindex(columns=columns).astype("float64")


def weather_features(days, cutoff_local: str, noaa_dir, forecast_dir, lag_days: int, margin_hours: float,
                     today: date | None = None) -> pd.DataFrame:
    """One row per day: wx_source, wx_known_by_utc, the wx_* values (observed up to OBSERVED_UNTIL,
    forecast after) and the observed values of day D - lag_days."""
    if lag_days < 2:
        raise ValueError("observed_weather_lag_days must be 2 or more: Logan's day D-1 ends after the cutoff")
    days = pd.DatetimeIndex(pd.to_datetime(pd.Series(days, dtype="datetime64[ns]")).unique()).sort_values()
    train = np.asarray(days <= OBSERVED_UNTIL)
    lagged = days - pd.Timedelta(days=lag_days)
    observed = read_observed(noaa_dir, set(days[train].year) | set(lagged.year), today)

    known = pd.Series(pd.NaT, index=days, dtype="datetime64[ns, UTC]")
    forecast = pd.DataFrame({name: np.nan for _, name in PAIRS.values()}, index=days)
    if not train.all():
        if forecast_dir is None:
            raise ValueError(f"days after {OBSERVED_UNTIL.date()} need #8's forecasts (forecast_dir)")
        later = days[~train]
        hourly = om.read_hourly(forecast_dir, om.years_for(later), today)
        daily = om.daily_forecasts(hourly, later, cutoff_utc(later, cutoff_local), margin_hours).set_index("date")
        known = daily["known_by_utc"].reindex(days)
        forecast = daily.reindex(days)

    out = pd.DataFrame({"date": days})
    out["wx_source"] = np.where(train, "observed", "forecast")
    out["wx_known_by_utc"] = known.reset_index(drop=True)
    for feature, (observed_name, forecast_name) in PAIRS.items():
        out[feature] = np.where(train, observed[observed_name].reindex(days).to_numpy(),
                                forecast[forecast_name].to_numpy(dtype="float64"))
    for name, column in zip(OBSERVED.values(), lag_names(lag_days)):
        out[column] = observed[name].reindex(lagged).to_numpy()
    return out
