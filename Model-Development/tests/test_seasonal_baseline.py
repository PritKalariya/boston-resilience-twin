"""Tests for the seasonal baseline (#106, ADR-005, ADR-027)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
from seasonal_baseline import SeasonalBaseline  # noqa: E402

D = pd.Timestamp("2024-01-15")


def history(surge_days=(), units=("Alpha",), first="2020-01-01", last="2024-12-31") -> pd.DataFrame:
    days = pd.date_range(first, last)
    frames = [pd.DataFrame({"unit": unit, "date": days, "label": days.isin(pd.to_datetime(list(surge_days))).astype("int8")})
              for unit in units]
    return pd.concat(frames, ignore_index=True)


def one(model: SeasonalBaseline, unit: str = "Alpha", day: pd.Timestamp = D) -> float:
    return float(model.predict_proba(pd.DataFrame({"unit": [unit], "date": [day]}))[0])


def test_the_probability_is_the_surge_share_of_the_same_weeks_in_3_previous_years():
    # one surge on Jan 15 of each previous year: 3 surges in the 3 x 29 = 87 window days
    model = SeasonalBaseline(history(["2021-01-15", "2022-01-15", "2023-01-15"]))
    assert one(model) == pytest.approx(3 / 87)


def test_days_outside_the_window_do_not_count():
    # Jan 1 and Jan 29 of 2023 are 14 days from Jan 15 2023 (inside); Dec 31 2022 is 15 days out
    model = SeasonalBaseline(history(["2023-01-01", "2023-01-29", "2022-12-31"]))
    assert one(model) == pytest.approx(2 / 87)


def test_it_never_reads_the_last_350_days():
    base = history(["2021-01-15", "2022-01-15", "2023-01-15"])
    expected = one(SeasonalBaseline(base))
    recent = base["date"].between(D - pd.Timedelta(days=350), D + pd.Timedelta(days=365))
    flipped = base.assign(label=np.where(recent, 1 - base["label"], base["label"]).astype("int8"))
    assert one(SeasonalBaseline(flipped)) == expected  # D itself, the days before it and every later day
    year_back = base.assign(label=np.where(base["date"] == D - pd.Timedelta(days=365), 0, base["label"]).astype("int8"))
    assert one(SeasonalBaseline(year_back)) == pytest.approx(2 / 87)  # 365 days back is read


def test_neighborhoods_do_not_share_history():
    both = pd.concat([history(["2023-01-15"], units=("Alpha",)), history(units=("Beta",))], ignore_index=True)
    model = SeasonalBaseline(both)
    assert one(model, "Alpha") == pytest.approx(1 / 87) and one(model, "Beta") == 0.0


def test_unlabeled_days_do_not_count():
    full = history(["2023-01-15"])
    sparse = full[~full["date"].between("2021-01-01", "2022-12-31")]  # only 2023's window days stay labeled
    assert one(SeasonalBaseline(sparse)) == pytest.approx(1 / 29)


def test_too_little_history_is_refused_not_guessed():
    short = history(first="2023-12-01")  # the 2023 window ends before the history starts
    with pytest.raises(ValueError, match="fewer than 20 labeled days"):
        one(SeasonalBaseline(short))
    with pytest.raises(ValueError, match="fewer than 20 labeled days"):
        one(SeasonalBaseline(history()), unit="Gamma")  # a neighborhood with no history


def test_fit_learns_nothing():
    model = SeasonalBaseline(history(["2023-01-15"]))
    before = one(model)
    assert model.fit(pd.DataFrame({"unit": ["Alpha"], "date": [D]}), np.array([1])) is model
    assert one(model) == before


def test_predictions_keep_the_row_order_across_neighborhoods():
    model = SeasonalBaseline(pd.concat([history(["2023-01-15"], units=("Alpha",)),
                                        history(["2022-01-15", "2023-01-15"], units=("Beta",))], ignore_index=True))
    rows = pd.DataFrame({"unit": ["Beta", "Alpha", "Beta"], "date": [D, D, D]})
    assert model.predict_proba(rows).tolist() == pytest.approx([2 / 87, 1 / 87, 2 / 87])


@pytest.mark.parametrize("broken, message", [
    (lambda h: h.drop(columns="label"), r"missing columns: \['label'\]"),
    (lambda h: h.head(0), "no rows"),
    (lambda h: h.assign(label=2), "0 or 1"),
    (lambda h: pd.concat([h, h.head(1)]), "repeats a neighborhood-day"),
])
def test_bad_history_is_refused(broken, message):
    with pytest.raises(ValueError, match=message):
        SeasonalBaseline(broken(history()))


def test_the_window_comes_from_the_settings():
    model = SeasonalBaseline(history(["2023-01-15"]), years=1, half_width=0, min_days=1)
    assert one(model) == 1.0  # one window day, a surge
