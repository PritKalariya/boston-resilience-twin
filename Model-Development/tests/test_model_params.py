"""Tests for the candidates' parameter files: the starting values and M6's tuned values (#107)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
import model_params as M  # noqa: E402
import training_data as D  # noqa: E402

LOGREG = "logreg:\n  C_grid: [0.01, 0.1, 1.0]\n  max_iter: 500\n  year_parts: 12\n"
XGBOOST = ("xgboost:\n  learning_rate: 0.1\n  max_rounds: 300\n  stopping_rounds: 20\n  max_depth: 3\n"
           "  min_child_weight: 1.0\n  reg_lambda: 1.0\n  subsample: 0.8\n  colsample_bytree: 0.8\n  seed: 0\n")
LIGHTGBM = ("lightgbm:\n  learning_rate: 0.2\n  max_rounds: 200\n  stopping_rounds: 10\n  num_leaves: 7\n"
            "  min_child_samples: 20\n  reg_lambda: 2.0\n  subsample: 0.7\n  subsample_freq: 1\n"
            "  colsample_bytree: 0.6\n  seed: 1\n")
GOOD = LOGREG + XGBOOST + LIGHTGBM


def test_the_committed_parameters_load():
    params = M.load(D.load_settings().model_params)
    assert set(params) == {"logreg", "xgboost", "lightgbm"}
    logreg, xgboost = params["logreg"], params["xgboost"]
    assert logreg["C_grid"][0] == 0.001 and logreg["C_grid"][-1] == 100.0 and len(logreg["C_grid"]) == 11
    assert (logreg["max_iter"], logreg["year_parts"]) == (2000, 12)
    assert xgboost == {"learning_rate": 0.05, "max_rounds": 3000, "stopping_rounds": 100, "max_depth": 4,
                       "min_child_weight": 5.0, "reg_lambda": 5.0, "subsample": 0.8, "colsample_bytree": 0.8,
                       "seed": 0}
    assert params["lightgbm"] == {"learning_rate": 0.05, "max_rounds": 3000, "stopping_rounds": 100,
                                  "num_leaves": 15, "min_child_samples": 100, "reg_lambda": 5.0, "subsample": 0.8,
                                  "subsample_freq": 1, "colsample_bytree": 0.8, "seed": 0}


def test_a_good_file_loads_with_its_values(tmp_path):
    path = tmp_path / "params.yaml"
    path.write_text(GOOD, encoding="utf-8")
    params = M.load(path)
    assert params["logreg"] == {"C_grid": (0.01, 0.1, 1.0), "max_iter": 500, "year_parts": 12}
    assert params["xgboost"]["learning_rate"] == 0.1 and params["xgboost"]["seed"] == 0
    assert params["lightgbm"]["num_leaves"] == 7 and params["lightgbm"]["subsample_freq"] == 1


@pytest.mark.parametrize("text, message", [
    ("", "must map each model to its parameters"),
    ("logreg: [\n", "is not valid YAML"),
    ("{}\n", r"has no parameters for \['lightgbm', 'logreg', 'xgboost'\]"),
    (GOOD + "forest:\n  trees: 10\n", r"unknown model\(s\) in params.yaml: \['forest'\]"),
    ("logreg: [0.1]\n" + XGBOOST + LIGHTGBM, "logreg in params.yaml must map each parameter"),
    (LOGREG + "  penalty: l1\n" + XGBOOST + LIGHTGBM, r"unknown parameter\(s\) for logreg in params.yaml: \['penalty'\]"),
    ("logreg:\n  C_grid: [0.1]\n  max_iter: 500\n" + XGBOOST + LIGHTGBM,
     r"logreg in params.yaml is missing \['year_parts'\]"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[]"), "logreg.C_grid must be a list of one or more numbers"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "0.1"), "logreg.C_grid must be a list of one or more numbers"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[0.0, 1.0]"), "logreg.C_grid must hold numbers above 0"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[1.0, 0.1]"), "logreg.C_grid must increase with no value twice"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[0.1, 0.1]"), "logreg.C_grid must increase with no value twice"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[1e-3, 1.0]"), "write decimals with a point"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[.nan]"), "logreg.C_grid must be a list of numbers"),
    (GOOD.replace("500", "0"), "logreg.max_iter must be a whole number of 1 or more"),
    (GOOD.replace("500", "true"), "logreg.max_iter must be a whole number of 1 or more"),
    (GOOD.replace("500", "2.5"), "logreg.max_iter must be a whole number of 1 or more"),
    (GOOD.replace("year_parts: 12", "year_parts: 2"), "logreg.year_parts must be a whole number of 3 or more"),
    (GOOD.replace("learning_rate: 0.1", "learning_rate: 0"), "xgboost.learning_rate must be above 0 and at most 1"),
    (GOOD.replace("subsample: 0.8", "subsample: 1.5"), "xgboost.subsample must be above 0 and at most 1"),
    (GOOD.replace("reg_lambda: 1.0", "reg_lambda: -1.0"), "xgboost.reg_lambda must be 0 or more"),
    (GOOD.replace("min_child_weight: 1.0", "min_child_weight: high"), "xgboost.min_child_weight must be a number"),
    (GOOD.replace("max_depth: 3", "max_depth: 0"), "xgboost.max_depth must be a whole number of 1 or more"),
    (GOOD.replace("seed: 0", "seed: -1"), "xgboost.seed must be a whole number of 0 or more"),
    (XGBOOST + "  gamma: 1.0\n" + LOGREG + LIGHTGBM, r"unknown parameter\(s\) for xgboost in params.yaml: \['gamma'\]"),
    (GOOD.replace("num_leaves: 7", "num_leaves: 1"), "lightgbm.num_leaves must be a whole number of 2 or more"),
    (GOOD.replace("subsample_freq: 1", "subsample_freq: -1"), "lightgbm.subsample_freq must be a whole number of 0"),
    (GOOD.replace("colsample_bytree: 0.6", "colsample_bytree: 0"), "lightgbm.colsample_bytree must be above 0"),
    (GOOD.replace("min_child_samples: 20", "min_child_samples: 0"), "lightgbm.min_child_samples must be a whole"),
])
def test_a_bad_file_is_refused_naming_the_problem(tmp_path, text, message):
    path = tmp_path / "params.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(M.ParamsError, match=message):
        M.load(path)


# --- tuned values (M6, #109) -------------------------------------------------


def _settings(tmp_path, tuned=None):
    """Settings of a temp repo; tuned, when given, is written to its tuned_params.yaml."""
    settings = D.load_settings(H.write_repo(tmp_path))
    if tuned is not None:
        settings.tuned_params.write_text(tuned, encoding="utf-8")
    return settings


def test_without_a_tuned_file_every_value_is_the_starting_one(tmp_path):
    settings = _settings(tmp_path)
    resolved = M.resolve(settings.model_params, settings.tuned_params)
    assert resolved.values == M.load(settings.model_params)
    assert resolved.tuned == {} and resolved.tuned_sha256 is None
    assert resolved.record("lightgbm") == {"values": resolved.values["lightgbm"], "tuned": [], "tuned_sha256": None}


def test_a_tuned_value_replaces_only_its_starting_value(tmp_path):
    tuned = "# M6 study, 50 trials\nlightgbm:\n  num_leaves: 31\n  learning_rate: 0.03\nlogreg:\n  C_grid: [0.1]\n"
    settings = _settings(tmp_path, tuned)
    resolved, starting = M.resolve(settings.model_params, settings.tuned_params), M.load(settings.model_params)
    assert resolved.values["lightgbm"] == {**starting["lightgbm"], "num_leaves": 31, "learning_rate": 0.03}
    assert resolved.values["logreg"] == {**starting["logreg"], "C_grid": (0.1,)}
    assert resolved.values["xgboost"] == starting["xgboost"]
    assert resolved.tuned == {"lightgbm": ["learning_rate", "num_leaves"], "logreg": ["C_grid"]}
    assert resolved.tuned_sha256 == "sha256:" + hashlib.sha256(tuned.encode("utf-8")).hexdigest()
    settings.tuned_params.write_bytes(tuned.replace("\n", "\r\n").encode("utf-8"))  # as a Windows checkout
    assert M.resolve(settings.model_params, settings.tuned_params).tuned_sha256 == resolved.tuned_sha256


def test_an_empty_tuned_file_sets_nothing(tmp_path):
    settings = _settings(tmp_path, "# nothing tuned yet\n")
    resolved = M.resolve(settings.model_params, settings.tuned_params)
    assert resolved.values == M.load(settings.model_params) and resolved.tuned == {}
    assert resolved.tuned_sha256.startswith("sha256:")


@pytest.mark.parametrize("text, message", [
    ("forest:\n  trees: 10\n", r"unknown model\(s\) in tuned_params.yaml: \['forest'\]"),
    ("lightgbm:\n  trees: 400\n", r"unknown parameter\(s\) for lightgbm in tuned_params.yaml: \['trees'\]"),
    ("lightgbm:\n  num_leaves: 1\n", "lightgbm.num_leaves must be a whole number of 2 or more"),
    ("lightgbm: 31\n", "lightgbm in tuned_params.yaml must map each parameter"),
    ("- lightgbm\n", "tuned_params.yaml must map each model to its parameters"),
    ("lightgbm: [\n", "tuned_params.yaml is not valid YAML"),
])
def test_a_bad_tuned_file_is_refused_naming_the_problem(tmp_path, text, message):
    settings = _settings(tmp_path, text)
    with pytest.raises(M.ParamsError, match=message):
        M.resolve(settings.model_params, settings.tuned_params)


def test_the_check_command_prints_every_parameter_and_marks_the_tuned_ones(tmp_path, capsys):
    config = H.write_repo(tmp_path)
    assert M.main(["--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "tuned values: none" in out and "(tuned)" not in out
    assert "lightgbm:\n  learning_rate: 0.05\n" in out and "  C_grid: [0.001, 0.00316, 0.01" in out
    (tmp_path / "tuned_params.yaml").write_text("xgboost:\n  max_depth: 6\n", encoding="utf-8")
    assert M.main(["--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "  max_depth: 6  (tuned)" in out and "(sha256:" in out and out.count("(tuned)") == 1


def test_the_check_command_exits_2_on_a_bad_file(tmp_path, capsys):
    config = H.write_repo(tmp_path)
    (tmp_path / "tuned_params.yaml").write_text("xgboost:\n  max_depth: 0\n", encoding="utf-8")
    assert M.main(["--config", str(config)]) == 2
    assert "xgboost.max_depth must be a whole number of 1 or more" in capsys.readouterr().err
