"""Tests for the training table and its splits (#106, ADR-003, ADR-027)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
import training_data as D  # noqa: E402


# --- settings --------------------------------------------------------------


def test_the_committed_settings_load_with_the_adr_003_splits():
    settings = D.load_settings()
    assert {split: (first.date().isoformat(), last.date().isoformat()) for split, (first, last) in settings.splits.items()} == {
        "train": ("2015-01-01", "2023-12-31"),
        "validation": ("2024-01-01", "2024-12-31"),
        "test": ("2025-01-01", "2026-06-30"),
    }
    assert settings.down_weight_years == (2020, 2021) and settings.down_weight == 0.5
    assert settings.inner_fold_years == (2019, 2022, 2023) and settings.missing_gap_max_points == 10
    assert settings.model_params == D.ROOT / "Model-Development" / "config" / "model_params.yaml"
    assert settings.tuned_params == D.ROOT / "Model-Development" / "config" / "tuned_params.yaml"
    assert settings.mlflow_store == D.ROOT / "Model-Development" / "mlruns"
    assert settings.mlflow_experiment == "model-training"
    assert settings.reports_dir == D.ROOT / "Model-Development" / "reports"
    assert settings.candidates == ("logreg", "xgboost", "lightgbm")
    assert settings.threshold_rule == "validation_f1_max"
    assert (settings.seasonal_years, settings.seasonal_half_width_days, settings.seasonal_min_days) == (3, 14, 20)
    assert settings.labels == D.ROOT / "Data-Pipeline" / "data" / "processed" / "labels.parquet"


@pytest.mark.parametrize("changes, message", [
    ({"validation_start": "2023-12-31"}, "must come after train_end"),
    ({"test_start": "2024-12-31"}, "must come after validation_end"),
    ({"test_end": "2024-12-31"}, "is before test_start"),
    ({"train_start": "2015-13-01"}, "train_start must be a date"),
    ({"down_weight": "0"}, "down_weight must be above 0"),
    ({"down_weight": "1.5"}, "down_weight must be above 0"),
    ({"down_weight": "half"}, "down_weight must be above 0"),
    ({"down_weight_years": "2020;2024"}, "2024 is outside the train window"),
    ({"down_weight_years": "2020;x"}, "down_weight_years must be a whole number"),
    ({"inner_fold_years": ""}, "inner_fold_years needs at least one year"),
    ({"inner_fold_years": "2019;x"}, "inner_fold_years must be a whole number"),
    ({"inner_fold_years": "2019;2024"}, "inner_fold_years: 2024 is outside the train window"),
    ({"inner_fold_years": "2015;2019"}, "inner_fold_years: 2015 has no train day before it"),
    ({"inner_fold_years": "2019;2020"}, "inner_fold_years: 2020 is down-weighted"),
    ({"inner_fold_years": "2023;2019"}, "inner_fold_years must increase with no year twice"),
    ({"inner_fold_years": "2019;2019"}, "inner_fold_years must increase with no year twice"),
    ({"missing_gap_max_points": "-1"}, "missing_gap_max_points must be a number from 0 to 100"),
    ({"missing_gap_max_points": "101"}, "missing_gap_max_points must be a number from 0 to 100"),
    ({"missing_gap_max_points": "ten"}, "missing_gap_max_points must be a number from 0 to 100"),
    ({"model_params": ""}, "model_params needs a path"),
    ({"tuned_params": ""}, "tuned_params needs a path"),
    ({"mlflow_store": ""}, "mlflow_store needs a path"),
    ({"mlflow_experiment": ""}, "mlflow_experiment needs a name"),
    ({"candidates": ""}, "candidates needs at least one model name"),
    ({"candidates": "logreg;xgboost;logreg"}, "candidates names a model twice"),
    ({"reports_dir": ""}, "reports_dir needs a path"),
    ({"threshold_rule": "fixed_half"}, "threshold_rule must be one of"),
    ({"seasonal_years": "0"}, "seasonal_years must be a whole number of 1 or more"),
    ({"seasonal_min_days": "88"}, "more than the window's 87 days"),
    ({"labels": ""}, "labels needs a path"),
    ({"runs_dir": None}, "missing setting(s)"),
    ({"extra_setting": "1"}, "unknown setting(s)"),
])
def test_a_bad_setting_is_refused_by_name(tmp_path, changes, message):
    config = H.write_repo(tmp_path, **changes)
    with pytest.raises(D.SettingsError, match=message.replace("(", r"\(").replace(")", r"\)")):
        D.load_settings(config)


def test_an_empty_down_weight_list_means_no_down_weighting(tmp_path):
    assert D.load_settings(H.write_repo(tmp_path, down_weight_years="")).down_weight_years == ()


def test_a_bad_header_or_a_repeated_setting_is_refused(tmp_path):
    config = tmp_path / "training.csv"
    config.write_text("name,value\nlabels,x\n", encoding="utf-8")
    with pytest.raises(D.SettingsError, match="header"):
        D.load_settings(config)
    config.write_text("setting,value\nlabels,a\nlabels,b\n", encoding="utf-8")
    with pytest.raises(D.SettingsError, match="duplicated"):
        D.load_settings(config)


# --- splits ----------------------------------------------------------------


def test_each_boundary_day_falls_in_exactly_one_split():
    splits = D.load_settings().splits
    days = pd.Series(pd.to_datetime(["2014-12-31", "2015-01-01", "2023-12-31", "2024-01-01", "2024-12-31",
                                      "2025-01-01", "2026-06-30", "2026-07-01"]))
    assert D.split_of(days, splits).tolist() == ["", "train", "train", "validation", "validation",
                                                  "test", "test", ""]


def test_no_row_crosses_splits(tmp_path):
    settings = D.load_settings(H.write_repo(tmp_path))
    table, _ = D.load_table(settings)
    assert set(table["split"]) == set(D.SPLITS)
    assert not table.duplicated(["unit", "date"]).any()
    for split, (first, last) in settings.splits.items():
        days = table.loc[table["split"] == split, "date"]
        assert days.min() >= first and days.max() <= last
    keys = {split: set(map(tuple, table.loc[table["split"] == split, ["unit", "date"]].to_numpy()))
            for split in D.SPLITS}
    assert not (keys["train"] & keys["validation"]) and not (keys["validation"] & keys["test"])
    assert not (keys["train"] & keys["test"])


def test_check_splits_catches_a_row_in_the_wrong_split(tmp_path):
    settings = D.load_settings(H.write_repo(tmp_path))
    table, _ = D.load_table(settings)
    table.loc[table.index[table["split"] == "validation"][0], "split"] = "train"
    with pytest.raises(ValueError, match="in the wrong split"):
        D.check_splits(table, settings.splits)


def test_check_splits_catches_a_day_in_two_splits(tmp_path):
    settings = D.load_settings(H.write_repo(tmp_path))
    table, _ = D.load_table(settings)
    doubled = pd.concat([table, table[table["split"] == "test"].head(1)], ignore_index=True)
    with pytest.raises(ValueError, match="appear twice"):
        D.check_splits(doubled, settings.splits)


# --- the table -------------------------------------------------------------


def test_the_table_keeps_labeled_days_in_a_split_and_down_weights_2020_and_2021(tmp_path):
    table, counts = D.load_table(D.load_settings(H.write_repo(tmp_path)))
    assert list(table.columns) == ["unit", "date", "split", "label", "weight", "day_of_week"]
    assert counts["unlabeled dropped"] == 12  # 6 days per unit with no baseline
    live = pd.date_range("2026-07-01", "2026-09-30")
    assert counts["outside every split dropped"] == 2 * len(live)
    assert table["label"].dtype == "int8" and set(table["label"]) == {0, 1}
    down = (table["split"] == "train") & table["date"].dt.year.isin([2020, 2021])
    assert (table.loc[down, "weight"] == 0.5).all() and (table.loc[~down, "weight"] == 1.0).all()
    assert counts["down-weighted train rows"] == int(down.sum()) > 0
    assert counts["validation surges"] == int(table.loc[table["split"] == "validation", "label"].sum()) > 0


def test_the_inputs_are_every_column_but_the_row_columns(tmp_path):
    table, _ = D.load_table(D.load_settings(H.write_repo(tmp_path)))
    assert D.input_columns(table) == ["day_of_week"]
    assert D.input_columns(table.assign(extra=1.0)) == ["day_of_week", "extra"]


def test_an_input_missing_far_more_often_outside_train_is_refused(tmp_path):
    table, _ = D.load_table(D.load_settings(H.write_repo(tmp_path)))
    validation = table.index[table["split"] == "validation"]
    table.loc[validation[: len(validation) // 2], "day_of_week"] = float("nan")
    with pytest.raises(ValueError, match="1 input\\(s\\) differ in missing share between train and validation by "
                                         "more than missing_gap_max_points \\(10\\), worst day_of_week: missing on "
                                         "50.0% of validation rows against 0.0% of train rows"):
        D.check_missing_gaps(table, ["day_of_week"], 10)
    D.check_missing_gaps(table, ["day_of_week"], 50)  # a gap at the limit passes


def test_a_test_split_gap_is_refused_too_and_small_gaps_pass(tmp_path):
    table, _ = D.load_table(D.load_settings(H.write_repo(tmp_path)))
    test = table.index[table["split"] == "test"]
    table.loc[test[: len(test) // 20], "day_of_week"] = float("nan")  # 5 percent of test
    D.check_missing_gaps(table, ["day_of_week"], 10)
    table.loc[test[: len(test) // 4], "day_of_week"] = float("nan")  # 25 percent of test
    with pytest.raises(ValueError, match="between train and test"):
        D.check_missing_gaps(table, ["day_of_week"], 10)


def test_only_the_catalog_model_inputs_are_kept():
    assert D.model_inputs(D.load_settings().feature_catalog)[:3] == ["day_of_week", "month", "day_of_year"]


def test_a_catalog_with_no_model_input_is_refused(tmp_path):
    catalog = tmp_path / "catalog.csv"
    catalog.write_text("feature,use_in_model\nunit,no\n", encoding="utf-8")
    with pytest.raises(ValueError, match="marks no feature"):
        D.model_inputs(catalog)
    catalog.write_text("feature,model\nunit,no\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        D.model_inputs(catalog)


def _broken(kind):
    labels = H.make_labels(first="2023-01-01", last="2025-03-31")
    features = H.make_features(labels)
    if kind == "empty labels":
        labels = labels.head(0)
    elif kind == "no surge column":
        labels = labels.drop(columns="surge")
    elif kind == "no model input":
        features = features.drop(columns="day_of_week")
    elif kind == "unparseable date":
        labels["date"] = labels["date"].dt.strftime("%Y-%m-%d").astype(object)
        labels.loc[3, "date"] = "2024-02-30"
    elif kind == "a time of day":
        labels.loc[3, "date"] = labels.loc[3, "date"] + pd.Timedelta(hours=6)
    elif kind == "a repeated day":
        labels = pd.concat([labels, labels.head(1)], ignore_index=True)
    elif kind == "keys disagree":
        features = features.iloc[1:]
    return labels, features


@pytest.mark.parametrize("kind, message", [
    ("empty labels", "has no rows"),
    ("no surge column", r"missing columns: \['surge'\]"),
    ("no model input", r"missing columns: \['day_of_week'\]"),
    ("unparseable date", "does not parse"),
    ("a time of day", "whole days"),
    ("a repeated day", "repeat a neighborhood-day"),
    ("keys disagree", "labels and features disagree on 1 neighborhood-days"),
])
def test_empty_and_malformed_inputs_are_refused_by_name(tmp_path, kind, message):
    labels, features = _broken(kind)
    settings = D.load_settings(H.write_repo(tmp_path, label_frame=labels, feature_frame=features))
    with pytest.raises(ValueError, match=message):
        D.load_table(settings)


def test_a_table_with_no_labeled_day_in_any_split_is_refused(tmp_path):
    labels = H.make_labels(first="2026-08-01", last="2026-09-30")  # only live days
    with pytest.raises(ValueError, match="no labeled row falls in any split"):
        D.load_table(D.load_settings(H.write_repo(tmp_path, label_frame=labels)))


# --- data version ----------------------------------------------------------


def test_the_data_version_ignores_line_endings_but_not_content(tmp_path):
    lf, crlf, other = tmp_path / "lf.lock", tmp_path / "crlf.lock", tmp_path / "other.lock"
    lf.write_bytes(b"schema: '2.0'\nstages: {}\n")
    crlf.write_bytes(b"schema: '2.0'\r\nstages: {}\r\n")
    other.write_bytes(b"schema: '2.0'\nstages: {a: 1}\n")
    assert D.data_version(lf) == D.data_version(crlf)
    assert D.data_version(lf) != D.data_version(other)
    assert D.data_version(lf).startswith("sha256:") and len(D.data_version(lf)) == len("sha256:") + 64
