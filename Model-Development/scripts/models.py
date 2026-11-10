"""The models train.py can train by name (#106, ADR-027): the seasonal baseline and the candidates of
M4 (#107), all behind one interface.

Every model has two methods:
- fit(rows, label, weight): learn from the train split's rows (unit, date, split, label, weight and
  the model inputs), their 0 or 1 labels and their sample weights (training_data.load_table);
- predict_proba(rows): one surge probability from 0 to 1 per row.
After fit, a candidate's details record what it chose on the inner folds (inner_folds.py), for run.json.

build(name, table, settings) makes a model. table is the whole labeled table of every split. Only a
history model such as the seasonal baseline reads its labels, and only into the past. A candidate
reads the table's columns to find its inputs and checks their missing shares across the splits
(training_data.check_missing_gaps); it learns from the rows fit gives it and nothing else.
"""

from __future__ import annotations

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, SplineTransformer, StandardScaler

import inner_folds
import model_params
import training_data
from seasonal_baseline import SeasonalBaseline

# How each input enters logistic regression, by name (any other input is a plain number).
CATEGORIES = ("day_of_week", "month")  # one column per value
YEAR_DAYS = {"day_of_year": 366}  # a smooth periodic curve over this many days, so 31 December meets 1 January
LOG_AMOUNTS = ("static_population", "static_land_sqmi", "static_density")


def _log_amount(column: str) -> bool:
    """Amounts with a long right tail enter as log(1 + x): the self-relative 311 ratios (*_rel*),
    precipitation and snowfall (*_mm), and a neighborhood's population, area and density."""
    return "_rel" in column or column.endswith("_mm") or column in LOG_AMOUNTS


def _fill_and_scale() -> list:
    """A missing value takes the median of the rows fit on, with a 0 or 1 column flagging it; then
    every column is scaled to mean 0 and standard deviation 1, so one C regularizes all alike."""
    return [("fill", SimpleImputer(strategy="median", add_indicator=True)), ("scale", StandardScaler())]


def logreg_preparation(inputs: list[str], year_parts: int) -> ColumnTransformer:
    """The preparation step of the logistic regression pipeline. Every part learns from the rows the
    pipeline is fit on only (categories, curve, medians, scales), so inside an inner fold nothing is
    learned from the year it is checked on."""
    categories = [c for c in inputs if c in CATEGORIES]
    curves = [c for c in inputs if c in YEAR_DAYS]
    logged = [c for c in inputs if _log_amount(c) and c not in categories + curves]
    plain = [c for c in inputs if c not in categories + curves + logged]
    parts = []
    if categories:
        parts.append(("category", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categories))
    for column in curves:
        knots = np.linspace(0, YEAR_DAYS[column], year_parts + 1).reshape(-1, 1)
        parts.append((column, SplineTransformer(knots=knots, degree=3, extrapolation="periodic",
                                                include_bias=False), [column]))
    if logged:
        log1p = FunctionTransformer(np.log1p, feature_names_out="one-to-one")
        parts.append(("log", Pipeline([("log1p", log1p), *_fill_and_scale()]), logged))
    if plain:
        parts.append(("plain", Pipeline(_fill_and_scale()), plain))
    return ColumnTransformer(parts)


class LogisticModel:
    """Logistic regression in one scikit-learn pipeline (preparation, then the model), with C picked
    on the inner folds by the lowest mean log loss and the pipeline then refit on every train day.
    No class reweighting: the shared threshold handles the rare surges, and reweighting would only
    distort the probabilities. The 2020 and 2021 sample weights reach the model in every fit."""

    def __init__(self, inputs: list[str], fold_years: tuple[int, ...], C_grid: tuple[float, ...], max_iter: int,
                 year_parts: int):
        self.inputs, self.fold_years = list(inputs), tuple(fold_years)
        self.C_grid, self.max_iter, self.year_parts = tuple(C_grid), max_iter, year_parts
        self.pipeline: Pipeline | None = None
        self.details: dict = {}

    def _pipeline(self) -> Pipeline:
        return Pipeline([("prepare", logreg_preparation(self.inputs, self.year_parts)),
                         ("model", LogisticRegression(max_iter=self.max_iter))])

    def fit(self, rows, label, weight) -> LogisticModel:
        label, weight = np.asarray(label), np.asarray(weight, dtype="float64")
        folds = inner_folds.make_folds(rows["date"], label, self.fold_years)
        search = GridSearchCV(self._pipeline(), {"model__C": list(self.C_grid)}, scoring="neg_log_loss",
                              cv=inner_folds.index_pairs(folds), refit=True, error_score="raise")
        search.fit(rows[self.inputs], label, model__sample_weight=weight)
        self.pipeline = search.best_estimator_
        fitted, results, best = self.pipeline.named_steps["model"], search.cv_results_, search.best_index_
        self.details = {
            "chosen": {"C": float(search.best_params_["model__C"])},
            "chosen_by": "lowest mean log loss over the inner folds",
            "inner_folds": inner_folds.describe(folds, rows["date"], label),
            "inner_log_loss_by_C": [[float(c), round(-float(score), 6)]  # [C, mean log loss] in grid order
                                    for c, score in zip(results["param_model__C"], results["mean_test_score"])],
            "inner_log_loss_by_fold": {str(fold.year): round(-float(results[f"split{k}_test_score"][best]), 6)
                                       for k, fold in enumerate(folds)},
            "prepared_columns": int(fitted.coef_.shape[1]),
            "converged": bool(fitted.n_iter_.max() < self.max_iter),
        }
        return self

    def predict_proba(self, rows) -> np.ndarray:
        if self.pipeline is None:
            raise ValueError("fit the model before predicting")
        return self.pipeline.predict_proba(rows[self.inputs])[:, 1]


def _inputs(table, settings) -> list[str]:
    """The table's model inputs, after checking their missing shares across the splits."""
    inputs = training_data.input_columns(table)
    training_data.check_missing_gaps(table, inputs, settings.missing_gap_max_points)
    return inputs


def _seasonal(table, settings) -> SeasonalBaseline:
    return SeasonalBaseline(table[["unit", "date", "label"]], years=settings.seasonal_years,
                            half_width=settings.seasonal_half_width_days, min_days=settings.seasonal_min_days)


def _logreg(table, settings) -> LogisticModel:
    inputs = _inputs(table, settings)
    return LogisticModel(inputs, settings.inner_fold_years, **model_params.load(settings.model_params)["logreg"])


MODELS = {"seasonal": _seasonal, "logreg": _logreg}


def build(name: str, table, settings):
    """The model registered under name; fails naming the known models."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known models: {sorted(MODELS)}")
    return MODELS[name](table, settings)
