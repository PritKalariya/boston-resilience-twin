"""The seasonal baseline (#106, ADR-027): the model every candidate must beat by 0.05 F1 (ADR-026).

Its surge probability for a neighborhood on day D is the share of surge days among that
neighborhood's labeled days in ADR-005's window: D plus or minus 14 days in each of the previous 3
years, the lags 365 * y + o for y from 1 to 3 and o from -14 to 14 (87 days), as
live_baselines.seasonal_baseline builds the surge baseline. Every lag is at least 351 days back, so
the probability never reads D or any day a forecast made the evening before could not know. A day
with fewer than 20 labeled days in its window has no probability (a label needs 20 history days
too, ADR-005), and predict_proba fails on it rather than guess.

It learns nothing from the training rows: fit keeps no state, and predict_proba reads the label
history it was built with. It uses no features, so it scores the same live as in a backtest. The
window sizes come from training.csv (seasonal_years, seasonal_half_width_days, seasonal_min_days).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class SeasonalBaseline:
    name = "seasonal"

    def __init__(self, history: pd.DataFrame, years: int = 3, half_width: int = 14, min_days: int = 20):
        """history: the labeled neighborhood-days (unit, date, label 0 or 1), of every split."""
        missing = {"unit", "date", "label"} - set(history.columns)
        if missing:
            raise ValueError(f"history is missing columns: {sorted(missing)}")
        if history.empty:
            raise ValueError("history has no rows")
        if not history["label"].isin([0, 1]).all():
            raise ValueError("history labels must be 0 or 1")
        if history.duplicated(["unit", "date"]).any():
            raise ValueError("history repeats a neighborhood-day")
        self.lags = [365 * y + o for y in range(1, years + 1) for o in range(-half_width, half_width + 1)]
        self.min_days = min_days
        self._history = {unit: rows.set_index(pd.to_datetime(rows["date"]))["label"].astype("float64")
                         for unit, rows in history.groupby("unit", sort=True)}

    def fit(self, rows: pd.DataFrame, label, weight=None) -> "SeasonalBaseline":
        """Nothing to learn: the probability is a fixed function of the label history."""
        return self

    def _shares(self, unit: str, days: pd.DatetimeIndex) -> pd.Series:
        """The window share of surge days for one neighborhood on each of days; NaN with too few labeled days."""
        labels = self._history.get(unit)
        if labels is None:
            return pd.Series(np.nan, index=days)
        calendar = pd.date_range(min(labels.index.min(), days.min()), max(labels.index.max(), days.max()))
        series = labels.reindex(calendar)  # unlabeled days, and days before the history, are NaN and never count
        stacked = np.vstack([series.shift(lag).to_numpy() for lag in self.lags])
        known = ~np.isnan(stacked)
        n = known.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(n >= self.min_days, np.nansum(stacked, axis=0) / np.maximum(n, 1), np.nan)
        return pd.Series(share, index=calendar).reindex(days)

    def predict_proba(self, rows: pd.DataFrame) -> np.ndarray:
        """The surge probability of each row (unit and date); fails on a row without enough history."""
        out = np.full(len(rows), np.nan)
        dates = pd.to_datetime(rows["date"]).to_numpy()
        for unit, positions in rows.groupby("unit", sort=False).indices.items():
            days = pd.DatetimeIndex(dates[positions])
            out[positions] = self._shares(unit, days).to_numpy()
        if np.isnan(out).any():
            first = rows.iloc[int(np.flatnonzero(np.isnan(out))[0])]
            raise ValueError(f"{int(np.isnan(out).sum())} rows have fewer than {self.min_days} labeled days in "
                             f"their seasonal window, for example {first['unit']} {pd.Timestamp(first['date']).date()}")
        return out
