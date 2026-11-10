"""Tests for the inner time folds on the train years (#107)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_validate

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
import inner_folds as F  # noqa: E402
import training_data as D  # noqa: E402


@pytest.fixture
def train_rows(tmp_path):
    """The train split of a temp repo whose labels start 2016-01-01 (the first 6 days per unit unlabeled),
    with the committed inner fold years."""
    settings = D.load_settings(H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01")))
    table, _ = D.load_table(settings)
    assert settings.inner_fold_years == (2019, 2022, 2023)
    return table[table["split"] == "train"].reset_index(drop=True)


def _folds(rows, years=(2019, 2022, 2023)):
    return F.make_folds(rows["date"], rows["label"], years)


def _surges(rows, first, last):
    days = rows["date"]
    return int(rows.loc[(days >= first) & (days <= last), "label"].sum())


def test_each_fold_fits_on_every_earlier_train_day_and_is_checked_on_its_year(train_rows):
    records = F.describe(_folds(train_rows), train_rows["date"], train_rows["label"])
    # 2 units: 360 days in 2016 after the 6 unlabeled ones, 365 or 366 in every later year
    expected = [(2019, "2018-12-31", 2 * 1090), (2022, "2021-12-31", 2 * 2186), (2023, "2022-12-31", 2 * 2551)]
    assert [r["year"] for r in records] == [year for year, _, _ in expected]
    for record, (year, fit_last, fit_rows) in zip(records, expected):
        assert (record["fit_first"], record["fit_last"], record["fit_rows"]) == ("2016-01-07", fit_last, fit_rows)
        assert (record["check_first"], record["check_last"], record["check_rows"]) == (
            f"{year}-01-01", f"{year}-12-31", 2 * 365)
        assert record["fit_surges"] == _surges(train_rows, "2016-01-01", fit_last)
        assert record["check_surges"] == _surges(train_rows, f"{year}-01-01", f"{year}-12-31")


def test_no_fold_sees_a_day_after_its_year_and_a_day_is_never_split(train_rows):
    for fold in _folds(train_rows):
        fit, check = train_rows.iloc[fold.fit], train_rows.iloc[fold.check]
        assert fit["date"].max() < check["date"].min()
        assert check["date"].max() <= pd.Timestamp(fold.year, 12, 31)
        assert not set(fit["date"]) & set(check["date"])
        # every neighborhood of a day is on the same side
        assert (fit.groupby("date")["unit"].nunique() == 2).all()
        assert (check.groupby("date")["unit"].nunique() == 2).all()


def test_the_down_weighted_years_are_fit_on_but_never_checked_on(train_rows):
    folds = _folds(train_rows)
    for fold in folds:
        assert (train_rows.iloc[fold.check]["weight"] == 1.0).all()
    fit = train_rows.iloc[folds[-1].fit]
    covid = fit["date"].dt.year.isin([2020, 2021])
    assert set(fit.loc[covid, "date"].dt.year) == {2020, 2021} and (fit.loc[covid, "weight"] == 0.5).all()


def test_folds_hold_positions_so_any_row_order_works(train_rows):
    shuffled = train_rows.sample(frac=1, random_state=0)  # index labels no longer match positions
    for fold in _folds(shuffled):
        assert (shuffled.iloc[fold.check]["date"].dt.year == fold.year).all()
        assert (shuffled.iloc[fold.fit]["date"].dt.year < fold.year).all()
        assert len(fold.fit) + len(fold.check) == (shuffled["date"].dt.year <= fold.year).sum()


def test_the_index_pairs_drive_scikit_learn_cross_validation(train_rows):
    pairs = F.index_pairs(_folds(train_rows))
    assert [len(check) for _, check in pairs] == [730, 730, 730]
    scores = cross_validate(LogisticRegression(), train_rows[["day_of_week"]], train_rows["label"], cv=pairs,
                            scoring="neg_log_loss")
    assert len(scores["test_score"]) == 3 and np.isfinite(scores["test_score"]).all()


@pytest.mark.parametrize("years, change, message", [
    ((2016,), None, "inner fold 2016 has no train day to fit on"),
    ((2019, 2024), None, "inner fold 2024 has no train day to check on"),
    ((2019,), "no surge in 2019", "inner fold 2019 has no surge to check on"),
    ((2019,), "only surges in 2019", "inner fold 2019 has no day without a surge to check on"),
    ((2019,), "no surge before 2019", "inner fold 2019 has no surge to fit on"),
    ((2022, 2019), None, "must increase with no year twice"),
    ((2019, 2019), None, "must increase with no year twice"),
    ((), None, "at least one year"),
])
def test_a_fold_that_cannot_score_a_model_is_refused_by_year(train_rows, years, change, message):
    rows = train_rows.copy()
    year = rows["date"].dt.year
    if change == "no surge in 2019":
        rows.loc[year == 2019, "label"] = 0
    elif change == "only surges in 2019":
        rows.loc[year == 2019, "label"] = 1
    elif change == "no surge before 2019":
        rows.loc[year < 2019, "label"] = 0
    with pytest.raises(ValueError, match=message):
        _folds(rows, years)


def test_labels_must_match_the_dates_one_to_one(train_rows):
    with pytest.raises(ValueError, match="labels for"):
        F.make_folds(train_rows["date"], train_rows["label"].iloc[1:], (2019,))
