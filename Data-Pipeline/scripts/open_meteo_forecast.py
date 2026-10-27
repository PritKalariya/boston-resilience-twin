"""Read #8's archived GFS day-before forecasts for the weather features (#16, ADR-003).

Everything this module knows about #8's files is in this one place, checked
against FORMAT_CHECKED: SET, TIME_FORMAT and VALUES.

#8 stores the Previous Runs API answer for Boston Logan byte for byte in
Data-Pipeline/data/raw/open_meteo/day_before_ncep_gfs_global/<year>.json:
hourly values in UTC (utc_offset_seconds 0), times as YYYY-MM-DDTHH:MM, wind in
m/s, and each value twice: <value>_previous_day1, what GFS predicted one day
before the hour, and <value>_previous_day2, two days before. Precipitation,
snowfall and gusts are stamped at the end of the hour they cover; temperature
is the value at the stamp.

Known before the cutoff. A previous_dayN value for hour h comes from the newest
GFS run (00, 06, 12 or 18 UTC) that started at least N days before h. Open-Meteo
documents it as the value "predicted 24 hours before valid time", and matching
the stored values with single GFS runs (Open-Meteo's Single Runs API, for
2026-04-15 and 2026-10-20, 48 of 48 hours) found runs 24 to 29 hours before h
for previous_day1 and 48 to 53 hours for previous_day2. A value counts as
available at its run's start plus forecast_issue_margin_hours (7): Open-Meteo's
run metadata showed the GFS 0.25 and 0.13 degree data of the 2026-10-26 12 UTC
run available 5.6 and 6.0 hours after it started, and Open-Meteo advises
waiting 10 more minutes. Day D uses a value only if it was available by D's
cutoff, 18:00 Boston time on D-1. So previous_day1 comes from runs up to
12 UTC on D-1, which give D's hours up to 17:00 UTC, previous_day2 covers the
rest, and a missing previous_day1 value falls back to previous_day2, never to
an observation. The delay was measured on one run, and runs before 2026-04-02
cannot be checked this way; the 12 UTC run would have to be more than 10 hours
late before it missed the cutoff.

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

from period_files import stored_periods

FORMAT_CHECKED = "#8 as merged (PR #76, main df56bd4) and the stored set c files for 2024 to 2026"
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
RUN_NS = 6 * HOUR_NS  # GFS starts a run every 6 hours, at 00, 06, 12 and 18 UTC
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
    yet), and its days stay missing, with a warning (period_files).
    """
    forecast_dir = Path(forecast_dir)
    this_year = (today or datetime.now(timezone.utc).date()).year
    years = sorted(year for year in set(years) if year >= FIRST_YEAR)
    stored = stored_periods(forecast_dir, years, this_year, f"run open_meteo.py --only {SET}",
                            "the forecast weather values (wx_* after 2023-12-31)")
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
    any hour behind a daily value the row keeps (none for a day left missing),
    so a check can prove that none came after the cutoff.
    """
    days = pd.DatetimeIndex(days)
    cut = np.repeat(pd.DatetimeIndex(cutoffs).tz_convert("UTC").as_unit("ns").asi8, HOURS_PER_DAY)
    starts = days.as_unit("ns").asi8 + STATION_DAY_START.value
    stamps = (starts[:, None] + HOUR_NS * np.arange(1, HOURS_PER_DAY + 1)[None, :]).ravel()
    lookup = hourly.set_axis(pd.DatetimeIndex(hourly.index).as_unit("ns")).reindex(
        pd.DatetimeIndex(stamps.astype("datetime64[ns]")).tz_localize("UTC"))
    margin = int(round(margin_hours * HOUR_NS))
    out = pd.DataFrame({"date": days})
    latest = np.full(len(days), NOT_USED)
    for feature, (name, _, factor, how) in VALUES.items():
        value, used_at = np.full(stamps.shape, np.nan), np.full(stamps.shape, NOT_USED)
        for n in DAYS_BEFORE:
            run = (stamps - n * DAY_NS) // RUN_NS * RUN_NS  # the GFS run the value comes from
            available = run + margin
            candidate = lookup[f"{name}_previous_day{n}"].to_numpy(dtype="float64") * factor
            take = np.isnan(value) & (available <= cut) & ~np.isnan(candidate)
            value[take], used_at[take] = candidate[take], available[take]
        per_day = value.reshape(len(days), HOURS_PER_DAY)  # a missing hour makes the day missing
        combined = getattr(per_day, how)(axis=1)
        out[feature] = combined.round(2) if how == "sum" else combined
        newest = used_at.reshape(len(days), HOURS_PER_DAY).max(axis=1)
        latest = np.maximum(latest, np.where(np.isnan(combined), NOT_USED, newest))  # only values the row keeps
    out["known_by_utc"] = pd.DatetimeIndex(latest.astype("datetime64[ns]")).tz_localize("UTC")
    return out
