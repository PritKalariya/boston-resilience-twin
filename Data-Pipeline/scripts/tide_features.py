"""Tide features per day (#16, ADR-003): NOAA's predicted water level at the Boston gauge (#7).

Predictions are astronomical, published years ahead, so they are known before
any cutoff and cover the training years (station 8443970, from 1921). #7
stores them in Data-Pipeline/data/raw/tides/predicted/<year>-H1.json (1 January
to 30 June) and <year>-H2.json (1 July to 31 December), exactly as CO-OPS
returned them: {"predictions": [{"t": "YYYY-MM-DD HH:MM", "v": "1.234"}]},
hourly, in GMT and metres above mean lower low water (MLLW).

Per day D, in Boston local time as 311 counts days (America/New_York):
tide_pred_hours is how many hourly predictions fall from D 00:00 up to D+1
00:00 (23, 24 or 25 with daylight saving), and tide_pred_max_m is the highest
of them, missing unless the day has all its hours.

Observed water levels (#7's `observed` folder) are never read: verified data
lags about a month and a day's own levels come only after the cutoff, so they
fail ADR-003's live rule and would leak.

Every half-year file from FIRST_YEAR up to the current half must be stored;
the current half may be absent (not downloaded yet), and its days stay
missing. Checked against TIDES_CHECKED: test_feature_sources.py reads what
tides.py itself stores.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from alert_history import day_start_utc

TIDES_CHECKED = "#7 as merged (tides.py, main df56bd4)"
FIRST_YEAR = 2015  # #7 downloads from 2015-01-01
TIME_FORMAT = "%Y-%m-%d %H:%M"
HOUR_NS = 3_600_000_000_000
TIDE_FEATURES = ["tide_pred_max_m"]
TIDE_META = ["tide_pred_hours"]


def half_year(day) -> str:
    """#7's file name for the half-year a day is in, for example 2026-H2."""
    return f"{day.year}-H{1 if day.month <= 6 else 2}"


def _read_half(path: Path) -> pd.Series:
    """One stored CO-OPS answer as hourly predictions; raises when it does not look like #7's file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path} is not JSON ({exc})") from exc
    rows = data.get("predictions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path} has no predictions list; recheck against #7 ({TIDES_CHECKED})")
    df = pd.DataFrame(rows, columns=["t", "v"])
    try:
        times = pd.to_datetime(df["t"].astype(str), format=TIME_FORMAT, utc=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: times are not {TIME_FORMAT} GMT; recheck against #7 ({TIDES_CHECKED})") from exc
    if (times.dt.minute != 0).any():
        raise ValueError(f"{path} is not hourly; recheck against #7 ({TIDES_CHECKED})")
    try:
        values = pd.to_numeric(df["v"].where(df["v"] != ""), errors="raise").astype("float64")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} has a value that is not a number") from exc
    return pd.Series(values.to_numpy(), index=pd.DatetimeIndex(times).as_unit("ns"), name="predicted_m")


def read_predictions(tides_dir, days, today: date | None = None) -> pd.Series:
    """Hourly predictions (metres above MLLW, indexed by UTC hour) for the half-years these days touch."""
    tides_dir = Path(tides_dir)
    days = pd.DatetimeIndex(days)
    current = half_year(today or datetime.now(timezone.utc).date())
    halves = sorted({half_year(day) for day in days.append(days + pd.Timedelta(days=1)) if day.year >= FIRST_YEAR})
    absent = [half for half in halves if not (tides_dir / f"{half}.json").is_file()]
    missing = [half for half in absent if half < current]
    if missing:
        raise FileNotFoundError(f"{tides_dir} lacks {', '.join(f'{half}.json' for half in missing)}; run tides.py (#7)")
    frames = [_read_half(tides_dir / f"{half}.json") for half in halves if half not in absent]
    if not frames:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC").as_unit("ns"), name="predicted_m")
    predictions = pd.concat(frames)
    if predictions.index.duplicated().any():
        raise ValueError(f"{tides_dir}: an hour is stored twice")
    return predictions.sort_index()


def tide_features(days, tides_dir, today: date | None = None) -> pd.DataFrame:
    """One row per day: tide_pred_hours and tide_pred_max_m over the Boston local day."""
    days = pd.DatetimeIndex(pd.to_datetime(pd.Series(days, dtype="datetime64[ns]")).unique()).sort_values()
    predictions = read_predictions(tides_dir, days, today).dropna()
    start = day_start_utc(days).as_unit("ns").asi8
    end = day_start_utc(days + pd.Timedelta(days=1)).as_unit("ns").asi8
    times, values = predictions.index.as_unit("ns").asi8, predictions.to_numpy()
    first, last = np.searchsorted(times, start, side="left"), np.searchsorted(times, end, side="left")
    hours = last - first
    peak = np.array([values[a:b].max() if b > a else np.nan for a, b in zip(first, last)], dtype="float64")
    return pd.DataFrame({"date": days, "tide_pred_hours": hours.astype("int64"),
                         "tide_pred_max_m": np.where(hours == (end - start) // HOUR_NS, peak, np.nan)})
