"""Tests for the candidate models behind the training interface (#107): logistic regression and XGBoost."""

from __future__ import annotations

import sys
from pathlib import Path

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


def test_build_makes_logreg_with_the_committed_parameters(table):
    model = models.build("logreg", table, D.load_settings())
    assert model.inputs == INPUTS and model.fold_years == (2019, 2022, 2023)
    assert (len(model.C_grid), model.max_iter, model.year_parts) == (11, 2000, 12)


def test_a_candidate_is_not_built_on_inputs_missing_far_more_often_outside_train(table):
    holes = table.copy()
    holes.loc[holes["split"] == "validation", "wx_snowfall_mm"] = np.nan  # the 2024 GFS snowfall gap
    with pytest.raises(ValueError, match="worst wx_snowfall_mm: missing on 100.0% of validation rows"):
        models.build("logreg", holes, D.load_settings())
    models.build("seasonal", holes, D.load_settings())  # the baseline reads no input


# --- XGBoost ---------------------------------------------------------------

XGB = dict(learning_rate=0.1, max_rounds=300, stopping_rounds=20, max_depth=3, min_child_weight=1.0, reg_lambda=1.0,
           subsample=0.8, colsample_bytree=0.8, seed=0)


def _fit_xgboost(table, **changes):
    rows = _train(table)
    model = models.XGBoostModel(INPUTS, (2019, 2022, 2023), **{**XGB, **changes})
    return model.fit(rows, rows["label"].to_numpy(), rows["weight"].to_numpy())


@pytest.fixture(scope="module")
def boosted(table):
    return _fit_xgboost(table)


def _validation(table):
    return table[table["split"] == "validation"].reset_index(drop=True)


def test_xgboost_reads_only_its_inputs(table, boosted):
    validation = _validation(table)
    changed = validation.assign(label=1 - validation["label"], weight=9.0, split="test", unit="Gamma", extra=1.0)
    assert boosted.model.get_booster().feature_names == INPUTS
    np.testing.assert_array_equal(boosted.predict_proba(validation), boosted.predict_proba(changed))


def test_xgboost_learns_the_signal_and_takes_missing_values_as_they_are(table, boosted):
    validation = _validation(table)
    probability = boosted.predict_proba(validation)
    assert probability.shape == (len(validation),) and ((probability > 0) & (probability < 1)).all()
    assert roc_auc_score(validation["label"], probability) > 0.8
    holes = validation.head(3).assign(wx_tmin_c=np.nan, snow_ice_rel7=np.nan)
    assert np.isfinite(boosted.predict_proba(holes)).all()


def test_xgboost_grows_the_tree_count_with_the_lowest_mean_fold_log_loss(table, boosted):
    rows = _train(table)
    grown, trees = boosted.details["trees_grown_in_search"], boosted.details["chosen"]["trees"]
    curves = []
    for fold in F.make_folds(rows["date"], rows["label"], (2019, 2022, 2023)):
        fit, check = rows.iloc[fold.fit], rows.iloc[fold.check]
        booster = xgb.train({**boosted.params, "seed": 0}, num_boost_round=grown,
                            dtrain=xgb.DMatrix(fit[INPUTS], label=fit["label"], weight=fit["weight"]))
        data = xgb.DMatrix(check[INPUTS])
        curves.append([log_loss(check["label"], booster.predict(data, iteration_range=(0, k)).astype("float64"))
                       for k in range(1, grown + 1)])
    mean = np.mean(curves, axis=0)
    np.testing.assert_allclose(boosted.inner_curve, mean, rtol=1e-6)
    assert trees == int(np.argmin(mean)) + 1 and grown == min(trees + 20, 300)
    assert boosted.details["inner_log_loss"] == pytest.approx(mean[trees - 1], abs=1e-6)
    assert [fold["year"] for fold in boosted.details["inner_folds"]] == [2019, 2022, 2023]


def test_xgboost_refits_on_every_train_day_with_the_chosen_trees(table, boosted):
    rows, validation = _train(table), _validation(table)
    trees = boosted.details["chosen"]["trees"]
    assert boosted.model.get_booster().num_boosted_rounds() == trees
    booster = xgb.train({**boosted.params, "seed": 0}, num_boost_round=trees,
                        dtrain=xgb.DMatrix(rows[INPUTS], label=rows["label"], weight=rows["weight"]))
    np.testing.assert_allclose(boosted.predict_proba(validation), booster.predict(xgb.DMatrix(validation[INPUTS])))
    unweighted = xgb.XGBClassifier(n_estimators=trees, random_state=0, **boosted.params).fit(rows[INPUTS], rows["label"])
    assert not np.allclose(unweighted.predict_proba(validation[INPUTS])[:, 1], boosted.predict_proba(validation))


def test_xgboost_is_repeatable(table, boosted):
    validation = _validation(table)
    np.testing.assert_array_equal(boosted.predict_proba(validation), _fit_xgboost(table).predict_proba(validation))


def test_a_tree_search_that_reaches_max_rounds_says_so(table):
    details = _fit_xgboost(table, max_rounds=5).details
    assert details["trees_grown_in_search"] == 5 and details["chosen"]["trees"] <= 5
    assert details["stopped_before_max_rounds"] is False


def test_xgboost_needs_fitting_before_predicting_and_builds_with_the_committed_parameters(table):
    with pytest.raises(ValueError, match="fit the model before predicting"):
        models.XGBoostModel(INPUTS, (2019,), **XGB).predict_proba(table)
    model = models.build("xgboost", table, D.load_settings())
    assert model.inputs == INPUTS and (model.max_rounds, model.stopping_rounds, model.seed) == (3000, 100, 0)
    assert model.params["learning_rate"] == 0.05 and model.params["max_depth"] == 4
