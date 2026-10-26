"""Read #8's archived GFS day-before forecasts for the weather features (#16, ADR-003).

Isolated on purpose: #8 (PR #76) is still changing, so everything this module
knows about #8's files is in this one place, written against FORMAT_CHECKED.
Recheck SET, TIME_FORMAT and VALUES against #8's final version before
integrating.

#8 stores the Previous Runs API answer for Boston Logan byte for byte in
Data-Pipeline/data/raw/open_meteo/day_before_ncep_gfs_global/<year>.json:
hourly values in UTC (utc_offset_seconds 0), times as YYYY-MM-DDTHH:MM, wind in
m/s, and each value twice: <value>_previous_day1, what GFS predicted about one
day before the hour, and <value>_previous_day2, about two days before.
Precipitation, snowfall and gusts are stamped at the end of the hour they
cover; temperature is the value at the stamp.

Known before the cutoff: Open-Meteo says how many days before the hour a value
was predicted, not when it became available, and GFS runs every 6 hours. So a
previous_dayN value for hour h counts as available at h - N days + the issue
margin (forecast_issue_margin_hours, 6), and day D uses it only if that is no
later than D's cutoff, 18:00 Boston time on D-1. With 6 hours, previous_day1
covers D's hours up to about noon and previous_day2 the rest; a missing
previous_day1 value falls back to previous_day2, never to an observation.

Daily values (daily_forecasts): Boston Logan's station day, midnight to
midnight Eastern standard time (05:00 to 05:00 UTC all year), the day GHCN-D's
observed values follow (#7 data card), so the observed training values and
these forecasts describe the same day. A day is missing unless all 24 of its
hours have a usable value, so the known gaps (every value before 2024-01-21,
snowfall before 2024-12-06) stay missing.

Data: Open-Meteo.com (CC BY 4.0), forecasts from NOAA NCEP GFS; #8 keeps the
attribution rows in docs/data/open-meteo.csv.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

FORMAT_CHECKED = "PR #76 (#8) at commit c6e2423"
SET = "day_before_ncep_gfs_global"  # #8's set c, the only one for validation and test
FIRST_YEAR = 2024  # set c starts on 2024-01-21
TIME_FORMAT = "%Y-%m-%dT%H:%M"
DAYS_BEFORE = (1, 2)  # _previous_day1, _previous_day2, newest first
# Daily value -> (Open-Meteo value, unit #8 stores, factor to the daily unit, how the day's hours combine)
VALUES = {
    "precip_mm": ("precipitation", "mm", 1.0, "sum"),
    "snowfall_mm": ("snowfall", "cm", 10.0, "sum"),
    "tmax_c": ("temperature_2m", "°C", 1.0, "max"),
    "tmin_c": ("temperature_2m", "°C", 1.0, "min"),
    "gust_max_ms": ("wind_gusts_10m", "m/s", 1.0, "max"),
}
STATION_DAY_START = pd.Timedelta(hours=5)  # midnight EST in UTC: Logan's GHCN-D day (#7 data card)
HOURS_PER_DAY = 24
HOUR_NS = 3_600_000_000_000
DAY_NS = 24 * HOUR_NS
NOT_USED = np.iinfo(np.int64).min  # pandas' missing time (NaT) as an integer


def columns() -> list[str]:
    """The hourly columns read from each file."""
    names = list(dict.fromkeys(name for name, *_ in VALUES.values()))
    return [f"{name}_previous_day{n}" for name in names for n in DAYS_BEFORE]


def years_for(days) -> list[int]:
    """The year files that hold the hours of these station days; a day's last hour is on D+1."""
    days = pd.DatetimeIndex(days)
    return sorted(set(days.year) | set((days + pd.Timedelta(days=1)).year))


def _read_year(path: Path) -> pd.DataFrame:
    """One stored answer as hourly rows; raises when it does not look like #8's set c."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path} is not JSON ({exc})") from exc
    hourly = data.get("hourly") if isinstance(data, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ValueError(f"{path} has no hourly time list; recheck against #8 ({FORMAT_CHECKED})")
    if data.get("utc_offset_seconds") != 0:
        raise ValueError(f"{path} is not in UTC (utc_offset_seconds {data.get('utc_offset_seconds')!r})")
    missing = [column for column in columns() if column not in hourly]
    if missing:
        raise ValueError(f"{path} lacks {missing}; recheck against #8 ({FORMAT_CHECKED})")
    units = data.get("hourly_units") or {}
    for name, unit, *_ in VALUES.values():
        for n in DAYS_BEFORE:
            column = f"{name}_previous_day{n}"
            if units.get(column) != unit:
                raise ValueError(f"{path}: {column} is in {units.get(column)!r}, expected {unit!r}; "
                                 f"recheck against #8 ({FORMAT_CHECKED})")
    try:
        times = pd.to_datetime(pd.Series(hourly["time"], dtype=object), format=TIME_FORMAT, utc=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: hourly times are not {TIME_FORMAT}; recheck against #8 ({FORMAT_CHECKED})") from exc
    frame = {}
    for column in columns():
        values = hourly[column]
        if not isinstance(values, list) or len(values) != len(times):
            count = len(values) if isinstance(values, list) else "no"
            raise ValueError(f"{path}: {column} has {count} values for {len(times)} hours")
        try:
            frame[column] = pd.Series(values, dtype="float64")  # null -> NaN, kept missing
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {column} has values that are not numbers") from exc
    return pd.DataFrame(frame).set_axis(pd.DatetimeIndex(times, name="valid_utc").as_unit("ns"))


def read_hourly(forecast_dir, years, today: date | None = None) -> pd.DataFrame:
    """Hourly forecasts of the given years, one row per UTC hour (index valid_utc), in #8's units.

    Years before FIRST_YEAR are not asked for. Every other year before the
    current one must be stored, since a gap in the stored set is a missing
    input, not missing weather; the current year may be absent (not downloaded
    yet), and its days stay missing.
    """
    forecast_dir = Path(forecast_dir)
    this_year = (today or datetime.now(timezone.utc).date()).year
    years = sorted(year for year in set(years) if year >= FIRST_YEAR)
    absent = [year for year in years if not (forecast_dir / f"{year}.json").is_file()]
    missing = [year for year in absent if year < this_year]
    if missing:
        raise FileNotFoundError(f"{forecast_dir} lacks {', '.join(f'{year}.json' for year in missing)}; "
                                f"run open_meteo.py --only {SET}")
    stored = [year for year in years if year not in absent]
    if not stored:
        return pd.DataFrame({column: pd.Series(dtype="float64") for column in columns()}).set_axis(
            pd.DatetimeIndex([], tz="UTC", name="valid_utc").as_unit("ns"))
    hourly = pd.concat([_read_year(forecast_dir / f"{year}.json") for year in stored])
    if hourly.index.duplicated().any():
        raise ValueError(f"{forecast_dir}: an hour is stored in two files")
    return hourly.sort_index()


def daily_forecasts(hourly: pd.DataFrame, days, cutoffs, margin_hours: float) -> pd.DataFrame:
    """Per day D: the VALUES over Logan's station day from forecasts available by D's cutoff.

    days are dates (midnight, no time zone); cutoffs are their forecast cutoffs
    in UTC, in the same order. known_by_utc is the latest availability time of
    any value used, so a check can prove that none came after the cutoff.
    """
    days = pd.DatetimeIndex(days)
    cut = np.repeat(pd.DatetimeIndex(cutoffs).tz_convert("UTC").as_unit("ns").asi8, HOURS_PER_DAY)
    starts = days.as_unit("ns").asi8 + STATION_DAY_START.value
    stamps = (starts[:, None] + HOUR_NS * np.arange(1, HOURS_PER_DAY + 1)[None, :]).ravel()
    lookup = hourly.set_axis(pd.DatetimeIndex(hourly.index).as_unit("ns")).reindex(
        pd.DatetimeIndex(stamps.astype("datetime64[ns]")).tz_localize("UTC"))
    margin = int(round(margin_hours * HOUR_NS))
    out = pd.DataFrame({"date": days})
    known = np.full(stamps.shape, NOT_USED)
    for feature, (name, _, factor, how) in VALUES.items():
        value, used_at = np.full(stamps.shape, np.nan), np.full(stamps.shape, NOT_USED)
        for n in DAYS_BEFORE:
            available = stamps - n * DAY_NS + margin
            candidate = lookup[f"{name}_previous_day{n}"].to_numpy(dtype="float64") * factor
            take = np.isnan(value) & (available <= cut) & ~np.isnan(candidate)
            value[take], used_at[take] = candidate[take], available[take]
        per_day = value.reshape(len(days), HOURS_PER_DAY)  # a missing hour makes the day missing
        combined = getattr(per_day, how)(axis=1)
        out[feature] = combined.round(2) if how == "sum" else combined
        known = np.maximum(known, used_at)
    latest = known.reshape(len(days), HOURS_PER_DAY).max(axis=1)
    out["known_by_utc"] = pd.DatetimeIndex(latest.astype("datetime64[ns]")).tz_localize("UTC")
    return out
