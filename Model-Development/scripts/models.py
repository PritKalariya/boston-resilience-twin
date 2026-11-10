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

import itertools

import lightgbm as lgb
import numpy as np
import xgboost as xgb
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


def _log_loss(label, probability, weight) -> float:
    """The weighted log loss of probabilities for 0 or 1 labels (as scikit-learn's log_loss)."""
    p = np.clip(np.asarray(probability, dtype="float64"), 1e-15, 1 - 1e-15)
    y = np.asarray(label, dtype="float64")
    return float(-np.average(y * np.log(p) + (1 - y) * np.log1p(-p), weights=weight))


def _xgboost_step(params: dict, fit_rows, fit_label, fit_weight, check_rows, check_label, check_weight):
    """One inner fold of the XGBoost tree search: a callable that grows the fold's booster by one tree
    and returns its log loss on the fold year. The booster's histogram bins come from the fold's fit
    rows only. Both data sets sit in the booster's cache, so each prediction adds only the new tree."""
    fit = xgb.DMatrix(fit_rows, label=fit_label, weight=fit_weight)
    check = xgb.DMatrix(check_rows, label=check_label, weight=check_weight)
    booster = xgb.Booster(params, [fit, check])
    rounds = itertools.count()

    def step() -> float:
        booster.update(fit, next(rounds))
        return _log_loss(check_label, booster.predict(check), check_weight)
    return step


def _lightgbm_step(params: dict, fit_rows, fit_label, fit_weight, check_rows, check_label, check_weight):
    """One inner fold of the LightGBM tree search, as _xgboost_step. The fold year is binned with the
    bins of the fit rows (reference), so the binning learns nothing from it. LightGBM scores the
    fold year after each tree itself, at full precision."""
    fit = lgb.Dataset(fit_rows, label=fit_label, weight=fit_weight)
    check = lgb.Dataset(check_rows, label=check_label, weight=check_weight, reference=fit)
    booster = lgb.Booster({**params, "metric": "binary_logloss"}, fit)
    booster.add_valid(check, "check")

    def step() -> float:
        booster.update()
        return booster.eval_valid()[0][2]
    return step


class _TreeSearchModel:
    """What XGBoost and LightGBM share. The number of trees comes from the inner folds
    (inner_folds.stop_early); the final model then grows that many trees on every train day, with the
    2020 and 2021 weights. Trees need no scaling, and a missing value takes the branch learned for it.
    No class reweighting, as for logistic regression. A subclass gives _fold_step and _final_model."""

    def __init__(self, inputs: list[str], fold_years: tuple[int, ...], max_rounds: int, stopping_rounds: int):
        self.inputs, self.fold_years = list(inputs), tuple(fold_years)
        self.max_rounds, self.stopping_rounds = max_rounds, stopping_rounds
        self.model = None
        self.inner_curve: list[float] = []  # the mean fold log loss after each tree of the search
        self.details: dict = {}

    def fit(self, rows, label, weight):
        label, weight = np.asarray(label), np.asarray(weight, dtype="float64")
        inputs = rows[self.inputs]
        folds = inner_folds.make_folds(rows["date"], label, self.fold_years)
        steps = [self._fold_step(inputs.iloc[f.fit], label[f.fit], weight[f.fit],
                                 inputs.iloc[f.check], label[f.check], weight[f.check]) for f in folds]
        trees, self.inner_curve, at_best = inner_folds.stop_early(steps, self.max_rounds, self.stopping_rounds)
        self.model = self._final_model(trees).fit(inputs, label, sample_weight=weight)
        self.details = {
            "chosen": {"trees": trees},
            "chosen_by": f"lowest mean log loss over the inner folds, searched until {self.stopping_rounds} "
                         "more trees brought no better mean",
            "inner_folds": inner_folds.describe(folds, rows["date"], label),
            "inner_log_loss": round(self.inner_curve[trees - 1], 6),
            "inner_log_loss_by_fold": {str(f.year): round(loss, 6) for f, loss in zip(folds, at_best)},
            "trees_grown_in_search": len(self.inner_curve),
            "stopped_before_max_rounds": len(self.inner_curve) < self.max_rounds,
        }
        return self

    def predict_proba(self, rows) -> np.ndarray:
        if self.model is None:
            raise ValueError("fit the model before predicting")
        return self.model.predict_proba(rows[self.inputs])[:, 1]


class XGBoostModel(_TreeSearchModel):
    """XGBoost: trees grown level by level up to max_depth, on histogram bins (tree_method hist)."""

    def __init__(self, inputs: list[str], fold_years: tuple[int, ...], learning_rate: float, max_rounds: int,
                 stopping_rounds: int, max_depth: int, min_child_weight: float, reg_lambda: float,
                 subsample: float, colsample_bytree: float, seed: int):
        super().__init__(inputs, fold_years, max_rounds, stopping_rounds)
        self.seed = seed
        self.params = {"objective": "binary:logistic", "tree_method": "hist", "learning_rate": learning_rate,
                       "max_depth": max_depth, "min_child_weight": min_child_weight, "reg_lambda": reg_lambda,
                       "subsample": subsample, "colsample_bytree": colsample_bytree}

    def _fold_step(self, *data):
        return _xgboost_step({**self.params, "seed": self.seed}, *data)

    def _final_model(self, trees: int) -> xgb.XGBClassifier:
        return xgb.XGBClassifier(n_estimators=trees, random_state=self.seed, **self.params)


class LightGBMModel(_TreeSearchModel):
    """LightGBM: trees grown leaf by leaf, so num_leaves bounds a tree's size. deterministic and
    force_row_wise make the same data and seed give the same model on every run."""

    def __init__(self, inputs: list[str], fold_years: tuple[int, ...], learning_rate: float, max_rounds: int,
                 stopping_rounds: int, num_leaves: int, min_child_samples: int, reg_lambda: float,
                 subsample: float, subsample_freq: int, colsample_bytree: float, seed: int):
        super().__init__(inputs, fold_years, max_rounds, stopping_rounds)
        self.seed = seed
        self.params = {"objective": "binary", "learning_rate": learning_rate, "num_leaves": num_leaves,
                       "min_child_samples": min_child_samples, "reg_lambda": reg_lambda, "subsample": subsample,
                       "subsample_freq": subsample_freq, "colsample_bytree": colsample_bytree, "random_state": seed,
                       "deterministic": True, "force_row_wise": True, "verbose": -1}

    def _fold_step(self, *data):
        return _lightgbm_step(self.params, *data)

    def _final_model(self, trees: int) -> lgb.LGBMClassifier:
        return lgb.LGBMClassifier(n_estimators=trees, **self.params)


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


def _xgboost(table, settings) -> XGBoostModel:
    inputs = _inputs(table, settings)
    return XGBoostModel(inputs, settings.inner_fold_years, **model_params.load(settings.model_params)["xgboost"])


def _lightgbm(table, settings) -> LightGBMModel:
    inputs = _inputs(table, settings)
    return LightGBMModel(inputs, settings.inner_fold_years, **model_params.load(settings.model_params)["lightgbm"])


MODELS = {"seasonal": _seasonal, "logreg": _logreg, "xgboost": _xgboost, "lightgbm": _lightgbm}


def build(name: str, table, settings):
    """The model registered under name; fails naming the known models."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known models: {sorted(MODELS)}")
    return MODELS[name](table, settings)
