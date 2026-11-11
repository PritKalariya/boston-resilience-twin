"""Tests for the candidate models behind the training interface (#107): logistic regression, XGBoost and LightGBM."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
import xgboost as xgb
from sklearn.metrics import log_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
import inner_folds as F  # noqa: E402
import models  # noqa: E402
import training_data as D  # noqa: E402

INPUTS = ["day_of_week", "month", "day_of_year", "snow_ice_rel7", "wx_snowfall_mm", "static_population", "wx_tmin_c"]
GRID = (0.01, 0.1, 1.0)


def make_table(first: str = "2016-01-01", seed: int = 0) -> pd.DataFrame:
    """A table like load_table's, train 2016 to 2023 and validation 2024, two units. Snowfall and the
    311 ratio rise on surge days; Beta has no population and the ratio is missing in January 2016."""
    labels = H.make_labels(first=first, last="2024-12-31", seed=seed)
    labels = labels[labels["surge"].notna()].reset_index(drop=True)
    rng = np.random.default_rng(seed)
    table = labels[["unit", "date"]].copy()
    table["split"] = np.where(table["date"].dt.year <= 2023, "train", "validation")
    table["label"] = labels["surge"].astype(bool).astype("int8")
    table["weight"] = np.where((table["split"] == "train") & table["date"].dt.year.isin([2020, 2021]), 0.5, 1.0)
    table["scored"] = True
    surge, n = table["label"].to_numpy(), len(table)
    table["day_of_week"] = table["date"].dt.dayofweek
    table["month"] = table["date"].dt.month
    table["day_of_year"] = table["date"].dt.dayofyear
    table["snow_ice_rel7"] = np.exp(rng.normal(0, 0.4, n) + 0.8 * surge)
    table.loc[table["date"] < "2016-02-01", "snow_ice_rel7"] = np.nan
    table["wx_snowfall_mm"] = np.where(surge == 1, rng.gamma(2.0, 8.0, n), rng.gamma(0.2, 2.0, n))
    table["static_population"] = table["unit"].map({"Alpha": 30000.0, "Beta": np.nan})
    table["wx_tmin_c"] = 5 - 10 * np.cos(2 * np.pi * table["day_of_year"] / 366) + rng.normal(0, 3, n)
    return table


def _settings(tmp_path, tuned=None):
    """The committed settings with tuned_params in tmp_path: absent, or holding tuned."""
    path = tmp_path / "tuned_params.yaml"
    if tuned is not None:
        path.write_text(tuned, encoding="utf-8")
    return dataclasses.replace(D.load_settings(), tuned_params=path)


@pytest.fixture(scope="module")
def table():
    return make_table()


def _train(table):
    return table[table["split"] == "train"].reset_index(drop=True)


def _fit(table, grid=GRID):
    rows = _train(table)
    model = models.LogisticModel(INPUTS, (2019, 2022, 2023), C_grid=grid, max_iter=2000, year_parts=12)
    return model.fit(rows, rows["label"].to_numpy(), rows["weight"].to_numpy())


@pytest.fixture(scope="module")
def fitted(table):
    return _fit(table)


def test_logreg_reads_only_its_inputs(table, fitted):
    validation = table[table["split"] == "validation"].reset_index(drop=True)
    changed = validation.assign(label=1 - validation["label"], weight=9.0, split="test", unit="Gamma", extra=1.0)
    assert list(fitted.pipeline.feature_names_in_) == INPUTS
    np.testing.assert_array_equal(fitted.predict_proba(validation), fitted.predict_proba(changed))


def test_logreg_learns_the_signal_and_fills_missing_values(table, fitted):
    validation = table[table["split"] == "validation"].reset_index(drop=True)
    probability = fitted.predict_proba(validation)
    assert probability.shape == (len(validation),) and ((probability > 0) & (probability < 1)).all()
    assert roc_auc_score(validation["label"], probability) > 0.8
    holes = validation.head(3).assign(wx_tmin_c=np.nan, snow_ice_rel7=np.nan)  # tmin is never missing in train
    assert np.isfinite(fitted.predict_proba(holes)).all()
    assert fitted.predict_proba(validation[validation["unit"] == "Beta"]).std() > 0  # no population, still varies


def test_logreg_picks_C_by_the_mean_log_loss_of_the_inner_folds(table, fitted):
    rows = _train(table)
    folds = F.make_folds(rows["date"], rows["label"], (2019, 2022, 2023))
    by_c = []
    for c in GRID:
        losses = []
        for fold in folds:
            pipeline = models.LogisticModel(INPUTS, (2019,), (c,), 2000, 12)._pipeline().set_params(model__C=c)
            fit = rows.iloc[fold.fit]
            pipeline.fit(fit[INPUTS], fit["label"], model__sample_weight=fit["weight"].to_numpy())
            check = rows.iloc[fold.check]
            losses.append(log_loss(check["label"], pipeline.predict_proba(check[INPUTS])[:, 1]))
        by_c.append([c, round(float(np.mean(losses)), 6)])
    assert fitted.details["inner_log_loss_by_C"] == by_c
    assert fitted.details["chosen"] == {"C": min(by_c, key=lambda pair: pair[1])[0]}
    assert [fold["year"] for fold in fitted.details["inner_folds"]] == [2019, 2022, 2023]
    assert set(fitted.details["inner_log_loss_by_fold"]) == {"2019", "2022", "2023"}
    assert fitted.details["converged"] is True


def test_logreg_refits_on_every_train_day_at_the_chosen_C(table, fitted):
    rows = _train(table)
    c = fitted.details["chosen"]["C"]
    pipeline = fitted._pipeline().set_params(model__C=c)
    pipeline.fit(rows[INPUTS], rows["label"], model__sample_weight=rows["weight"].to_numpy())
    final = fitted.pipeline.named_steps["model"]
    assert final.C == c
    np.testing.assert_allclose(final.coef_, pipeline.named_steps["model"].coef_)


def test_the_down_weighted_years_reach_the_model(table, fitted):
    rows = _train(table)
    c = fitted.details["chosen"]["C"]
    unweighted = fitted._pipeline().set_params(model__C=c).fit(rows[INPUTS], rows["label"])
    assert not np.allclose(unweighted.named_steps["model"].coef_, fitted.pipeline.named_steps["model"].coef_)


def test_logreg_is_repeatable(table, fitted):
    validation = table[table["split"] == "validation"]
    np.testing.assert_array_equal(fitted.predict_proba(validation), _fit(table).predict_proba(validation))


def test_each_input_is_prepared_by_its_kind(table):
    rows = _train(table)
    preparation = models.logreg_preparation(INPUTS, year_parts=12).fit(rows[INPUTS])
    names = list(preparation.get_feature_names_out())
    assert [n for n in names if n.startswith("category__day_of_week")] == [f"category__day_of_week_{d}" for d in range(7)]
    assert len([n for n in names if n.startswith("category__month")]) == 12
    assert len([n for n in names if n.startswith("day_of_year__")]) == 11  # 12 parts, one dropped as the intercept's
    assert [n for n in names if n.startswith("log__")] == [
        "log__snow_ice_rel7", "log__wx_snowfall_mm", "log__static_population",
        "log__missingindicator_snow_ice_rel7", "log__missingindicator_static_population"]
    assert [n for n in names if n.startswith("plain__")] == ["plain__wx_tmin_c"]
    curve = models.logreg_preparation(["day_of_year"], year_parts=12).fit(pd.DataFrame({"day_of_year": range(1, 367)}))
    days = curve.transform(pd.DataFrame({"day_of_year": [1, 366, 183]}))
    assert np.abs(days[0] - days[1]).max() < 0.05 < np.abs(days[0] - days[2]).max()  # 31 December meets 1 January


def test_three_year_parts_is_the_fewest_the_periodic_curve_takes():
    """year_parts + 1 knots, and a periodic cubic spline needs more than 3, so model_params.py
    refuses fewer than 3 parts before scikit-learn would."""
    year = pd.DataFrame({"day_of_year": range(1, 367)})
    assert models.logreg_preparation(["day_of_year"], year_parts=3).fit_transform(year).shape[0] == 366
    with pytest.raises(ValueError, match="Periodic splines require degree < n_knots"):
        models.logreg_preparation(["day_of_year"], year_parts=2).fit(year)


def test_the_ratio_enters_as_the_scaled_log(table):
    rows = _train(table)
    preparation = models.logreg_preparation(["snow_ice_rel7"], year_parts=12).fit(rows[["snow_ice_rel7"]])
    logged = np.log1p(rows["snow_ice_rel7"])
    filled = logged.fillna(logged.median())
    expected = (filled - filled.mean()) / filled.std(ddof=0)
    np.testing.assert_allclose(preparation.transform(rows[["snow_ice_rel7"]])[:, 0], expected)


def test_a_fold_year_with_nothing_before_it_stops_the_fit():
    rows = _train(make_table(first="2019-01-01"))
    model = models.LogisticModel(INPUTS, (2019, 2022), GRID, 2000, 12)
    with pytest.raises(ValueError, match="inner fold 2019 has no train day to fit on"):
        model.fit(rows, rows["label"].to_numpy(), rows["weight"].to_numpy())


def test_predicting_before_fitting_is_refused(table):
    with pytest.raises(ValueError, match="fit the model before predicting"):
        models.LogisticModel(INPUTS, (2019,), GRID, 2000, 12).predict_proba(table)


def test_build_makes_logreg_with_the_committed_parameters(table, tmp_path):
    model = models.build("logreg", table, _settings(tmp_path))
    assert model.inputs == INPUTS and model.fold_years == (2019, 2022, 2023)
    assert (len(model.C_grid), model.max_iter, model.year_parts) == (11, 2000, 12)


def test_a_candidate_is_not_built_on_inputs_missing_far_more_often_outside_train(table, tmp_path):
    holes = table.copy()
    holes.loc[holes["split"] == "validation", "snow_ice_rel7"] = np.nan  # missing in January 2016 too
    with pytest.raises(ValueError, match="worst snow_ice_rel7: missing on 100.0% of validation rows"):
        models.build("logreg", holes, _settings(tmp_path))
    models.build("seasonal", holes, _settings(tmp_path))  # the baseline reads no input


# --- XGBoost and LightGBM --------------------------------------------------

TREES = {
    "xgboost": (models.XGBoostModel, dict(learning_rate=0.1, max_rounds=300, stopping_rounds=20, max_depth=3,
                                          min_child_weight=1.0, reg_lambda=1.0, subsample=0.8, colsample_bytree=0.8,
                                          seed=0)),
    "lightgbm": (models.LightGBMModel, dict(learning_rate=0.1, max_rounds=300, stopping_rounds=20, num_leaves=7,
                                            min_child_samples=20, reg_lambda=1.0, subsample=0.8, subsample_freq=1,
                                            colsample_bytree=0.8, seed=0)),
}


def _fit_tree(name, table, **changes):
    model_class, params = TREES[name]
    rows = _train(table)
    model = model_class(INPUTS, (2019, 2022, 2023), **{**params, **changes})
    return model.fit(rows, rows["label"].to_numpy(), rows["weight"].to_numpy())


@pytest.fixture(scope="module", params=sorted(TREES))
def tree(request, table):
    """(name, a fitted tree model) for each of XGBoost and LightGBM."""
    return request.param, _fit_tree(request.param, table)


def _library_booster(name, model, rows, trees):
    """A booster trained by the library's own train function on rows with the model's settings."""
    if name == "xgboost":
        data = xgb.DMatrix(rows[INPUTS], label=rows["label"], weight=rows["weight"])
        return xgb.train({**model.params, "seed": model.seed}, data, num_boost_round=trees)
    data = lgb.Dataset(rows[INPUTS], label=rows["label"], weight=rows["weight"])
    return lgb.train(model.params, data, num_boost_round=trees)


def _library_predict(name, booster, rows, trees):
    """The booster's probabilities from its first trees trees."""
    if name == "xgboost":
        return booster.predict(xgb.DMatrix(rows[INPUTS]), iteration_range=(0, trees)).astype("float64")
    return booster.predict(rows[INPUTS], num_iteration=trees)


def _validation(table):
    return table[table["split"] == "validation"].reset_index(drop=True)


def test_a_tree_model_reads_only_its_inputs(table, tree):
    _, model = tree
    validation = _validation(table)
    changed = validation.assign(label=1 - validation["label"], weight=9.0, split="test", unit="Gamma", extra=1.0)
    assert list(model.model.feature_names_in_) == INPUTS
    np.testing.assert_array_equal(model.predict_proba(validation), model.predict_proba(changed))


def test_a_tree_model_learns_the_signal(table, tree):
    _, model = tree
    validation = _validation(table)
    probability = model.predict_proba(validation)
    assert probability.shape == (len(validation),) and ((probability > 0) & (probability < 1)).all()
    assert roc_auc_score(validation["label"], probability) > 0.8


@pytest.mark.parametrize("name", sorted(TREES))
def test_a_tree_model_takes_the_branch_it_learned_for_an_input_missing_in_train(table, name):
    """snow_ice_rel7 is missing on half the surge rows of every split and nowhere else, so a missing
    value means a surge: the branch learned for it must score those rows far above the rest."""
    rng = np.random.default_rng(3)
    holes = (table["label"] == 1).to_numpy() & (rng.random(len(table)) < 0.5)
    signal = table.assign(snow_ice_rel7=table["snow_ice_rel7"].mask(holes), wx_snowfall_mm=1.0)
    model = _fit_tree(name, signal)
    validation = _validation(signal)
    probability = model.predict_proba(validation)
    missing = validation["snow_ice_rel7"].isna().to_numpy()
    assert missing.sum() > 20 and probability[missing].mean() > 3 * probability[~missing].mean()


@pytest.mark.parametrize("name", sorted(TREES) + ["logreg"])
def test_a_candidate_refuses_an_input_never_missing_in_train_but_missing_on_a_scored_day(table, name, tmp_path):
    """wx_tmin_c is never missing in train: no candidate has learned what a missing value means."""
    hole = table.copy()
    day = hole.index[hole["date"] == "2024-01-01"]  # both neighborhoods
    hole.loc[day, "wx_tmin_c"] = np.nan
    with pytest.raises(ValueError, match="1 input\\(s\\) never missing in train are missing on scored validation "
                                         "rows, first wx_tmin_c: on 1 days from 2024-01-01 to 2024-01-01"):
        models.build(name, hole, _settings(tmp_path))
    hole.loc[day, "scored"] = False  # the day is not scored: nothing to refuse
    assert models.build(name, hole, _settings(tmp_path)).inputs == INPUTS


def test_a_tree_model_grows_the_tree_count_with_the_lowest_mean_fold_log_loss(table, tree):
    name, model = tree
    rows = _train(table)
    grown, trees = model.details["trees_grown_in_search"], model.details["chosen"]["trees"]
    curves = []
    for fold in F.make_folds(rows["date"], rows["label"], (2019, 2022, 2023)):
        booster = _library_booster(name, model, rows.iloc[fold.fit], grown)  # bins from the fit rows only
        check = rows.iloc[fold.check]
        curves.append([log_loss(check["label"], _library_predict(name, booster, check, k)) for k in range(1, grown + 1)])
    mean = np.mean(curves, axis=0)
    np.testing.assert_allclose(model.inner_curve, mean, rtol=1e-6)
    assert trees == int(np.argmin(mean)) + 1 and grown == min(trees + 20, 300)
    assert model.details["inner_log_loss"] == pytest.approx(mean[trees - 1], abs=1e-6)
    assert [fold["year"] for fold in model.details["inner_folds"]] == [2019, 2022, 2023]


def test_a_tree_model_refits_on_every_train_day_with_the_chosen_trees_and_weights(table, tree):
    name, model = tree
    rows, validation = _train(table), _validation(table)
    trees = model.details["chosen"]["trees"]
    booster = _library_booster(name, model, rows, trees)
    np.testing.assert_allclose(model.predict_proba(validation), _library_predict(name, booster, validation, trees),
                               rtol=1e-6)
    unweighted = model._final_model(trees).fit(rows[INPUTS], rows["label"])
    assert not np.allclose(unweighted.predict_proba(validation[INPUTS])[:, 1], model.predict_proba(validation))


def test_a_tree_model_is_repeatable(table, tree):
    name, model = tree
    validation = _validation(table)
    np.testing.assert_array_equal(model.predict_proba(validation), _fit_tree(name, table).predict_proba(validation))


@pytest.mark.parametrize("name", sorted(TREES))
def test_a_tree_search_that_reaches_max_rounds_says_so(table, name):
    details = _fit_tree(name, table, max_rounds=5).details
    assert details["trees_grown_in_search"] == 5 and details["chosen"]["trees"] <= 5
    assert details["stopped_before_max_rounds"] is False


@pytest.mark.parametrize("name, committed", [
    ("xgboost", {"learning_rate": 0.05, "max_depth": 4, "min_child_weight": 5.0}),
    ("lightgbm", {"learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 100, "deterministic": True}),
])
def test_a_tree_model_needs_fitting_and_builds_with_the_committed_parameters(table, tmp_path, name, committed):
    model_class, params = TREES[name]
    with pytest.raises(ValueError, match="fit the model before predicting"):
        model_class(INPUTS, (2019,), **params).predict_proba(table)
    model = models.build(name, table, _settings(tmp_path))
    assert model.inputs == INPUTS and (model.max_rounds, model.stopping_rounds, model.seed) == (3000, 100, 0)
    assert {key: model.params[key] for key in committed} == committed
    assert model.parameters["tuned"] == [] and model.parameters["tuned_sha256"] is None


def test_build_uses_the_tuned_values_over_the_starting_ones(table, tmp_path):
    settings = _settings(tmp_path, "lightgbm:\n  num_leaves: 31\n  stopping_rounds: 50\n")
    model = models.build("lightgbm", table, settings)
    assert model.params["num_leaves"] == 31 and model.stopping_rounds == 50 and model.params["learning_rate"] == 0.05
    assert model.parameters["tuned"] == ["num_leaves", "stopping_rounds"]
    assert model.parameters["values"]["num_leaves"] == 31 and model.parameters["tuned_sha256"].startswith("sha256:")
    other = models.build("xgboost", table, settings)  # the file names no xgboost value
    assert other.parameters["tuned"] == [] and other.parameters["tuned_sha256"] == model.parameters["tuned_sha256"]
