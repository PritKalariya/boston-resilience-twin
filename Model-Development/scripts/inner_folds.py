"""Inner time folds on the train years (#107): where a candidate picks its own settings.

A tree model needs its number of trees (early stopping) and logistic regression its
regularization strength C. Picking them on the validation year would let 2024 choose twice,
once for these settings and again for the threshold and the winner, so its F1 would read better
than it is (Cawley and Talbot, JMLR 2010). They are picked instead on expanding-window folds
inside the train split, one per year in inner_fold_years (training.csv):

- fold Y fits on every train day before 1 January of Y and is checked on the train days of Y;
- the folds cut by date, so the 24 neighborhoods of a day are always on the same side;
- no fold sees a day after its year, as the outer splits never do (ADR-003);
- the down-weighted years (2020 and 2021) are fit on with their weight, never checked on.

The committed years are 2019, 2022 and 2023. 2019 is the first year with three winters before it
(labels start 2016-01-06); 2022 and 2023 are the normal years closest to validation and test.
Earlier years would leave a fold one or two years to fit on, far from the eight the final model gets.

A fold holds row positions in the rows it was made from, not index labels, so make_folds can be
called with any slice of the training table. index_pairs gives the folds in the form scikit-learn's
cv argument and the folds argument of lightgbm.cv and xgboost.cv take.

stop_early picks a boosted model's number of trees on the folds: one model per fold grows a tree
at a time, and the count with the lowest mean log loss over the fold years wins.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, eq=False)
class Fold:
    year: int
    fit: np.ndarray    # positions of the rows dated before 1 January of year
    check: np.ndarray  # positions of the rows dated in year


def make_folds(dates, label, years: Sequence[int]) -> list[Fold]:
    """One fold per year, in order. dates and label are the train rows' dates and 0 or 1 labels.

    Fails, naming the year, when a fold has no row to fit on or to check on, or when one of its
    parts has no surge or no day without one: such a fold cannot score a model.
    """
    days = pd.to_datetime(pd.Series(dates)).to_numpy()
    label = np.asarray(label)
    if len(label) != len(days):
        raise ValueError(f"{len(label)} labels for {len(days)} dates")
    if not years:
        raise ValueError("the inner folds need at least one year")
    if list(years) != sorted(set(years)):
        raise ValueError(f"inner fold years must increase with no year twice, got {list(years)}")

    folds = []
    for year in years:
        start, end = np.datetime64(f"{year}-01-01"), np.datetime64(f"{year + 1}-01-01")
        fold = Fold(int(year), np.flatnonzero(days < start), np.flatnonzero((days >= start) & (days < end)))
        for part, rows in (("fit on", fold.fit), ("check on", fold.check)):
            if not len(rows):
                raise ValueError(f"inner fold {year} has no train day to {part}")
            surges = int(label[rows].sum())
            if surges == 0:
                raise ValueError(f"inner fold {year} has no surge to {part}")
            if surges == len(rows):
                raise ValueError(f"inner fold {year} has no day without a surge to {part}")
        folds.append(fold)
    return folds


def index_pairs(folds: Sequence[Fold]) -> list[tuple[np.ndarray, np.ndarray]]:
    """The folds as (fit, check) position pairs: the cv argument of scikit-learn's GridSearchCV and
    cross_validate, and the folds argument of lightgbm.cv and xgboost.cv."""
    return [(fold.fit, fold.check) for fold in folds]


def stop_early(steps: Sequence[Callable[[], float]], max_rounds: int,
               stopping_rounds: int) -> tuple[int, list[float], list[float]]:
    """The number of rounds (trees) with the lowest mean log loss over the folds.

    steps holds one callable per fold: each call grows that fold's model by one round and returns its
    log loss on the fold year. The folds grow together; after every round their losses are averaged.
    The search stops stopping_rounds rounds after the best mean, or at max_rounds. A tie keeps the
    earlier round, so the model gets fewer trees. The same rule as lightgbm.cv and xgboost.cv.

    Returns the best round (counted from 1), the mean loss of every round grown, and each fold's
    loss at the best round. Fails on a loss that is not a finite number.
    """
    curve: list[float] = []
    by_round: list[list[float]] = []
    best = 0
    for round_number in range(1, max_rounds + 1):
        losses = [float(step()) for step in steps]
        if not np.isfinite(losses).all():
            raise ValueError(f"round {round_number} gave a log loss that is not a finite number: {losses}")
        by_round.append(losses)
        curve.append(float(np.mean(losses)))
        if best == 0 or curve[-1] < curve[best - 1]:
            best = round_number
        elif round_number - best >= stopping_rounds:
            break
    return best, curve, by_round[best - 1]


def describe(folds: Sequence[Fold], dates, label) -> list[dict]:
    """One record per fold for run.json and MLflow: its year, and the first day, last day, rows and
    surges of the part it fits on and of the part it is checked on."""
    days = pd.to_datetime(pd.Series(dates)).reset_index(drop=True)
    label = np.asarray(label)
    out = []
    for fold in folds:
        record = {"year": fold.year}
        for part, rows in (("fit", fold.fit), ("check", fold.check)):
            record.update({f"{part}_first": str(days.iloc[rows].min().date()),
                           f"{part}_last": str(days.iloc[rows].max().date()),
                           f"{part}_rows": int(len(rows)), f"{part}_surges": int(label[rows].sum())})
        out.append(record)
    return out
