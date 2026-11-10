"""End-to-end tests of train.py and evaluate.py on a temp repo (#106, ADR-026, ADR-027)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
import evaluate  # noqa: E402
import models  # noqa: E402
import predictions as P  # noqa: E402
import train  # noqa: E402
import training_data as D  # noqa: E402


@pytest.fixture
def run_dir(tmp_path):
    config = H.write_repo(tmp_path)
    assert train.main(["--model", "seasonal", "--config", str(config), "--run-id", "r1"]) == 0
    return tmp_path / "runs" / "r1"


def test_a_run_writes_the_contract_files(run_dir, tmp_path):
    assert sorted(p.name for p in run_dir.iterdir()) == ["metrics.json", "predictions.parquet", "run.json",
                                                         "validation_counts.json"]
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    frame = P.read_predictions(run_dir / "predictions.parquet", threshold=run["threshold"])
    assert set(frame["split"]) == {"validation", "test"} and set(frame["model"]) == {"seasonal"}
    assert run["data_version"] == D.data_version(tmp_path / "data" / "dvc.lock")
    assert run["inputs"] == ["day_of_week"] and run["threshold_rule"] == "validation_f1_max"
    assert set(run["git"]) == {"commit", "uncommitted_changes"}
    assert run["settings"]["splits"]["test"] == ["2025-01-01", "2026-06-30"]
    assert run["model_details"] == {} and run["parameters"] == {}  # the baseline has no parameters


def test_validation_counts_are_the_gates_block_at_the_shared_threshold(run_dir):
    counts = json.loads((run_dir / "validation_counts.json").read_text(encoding="utf-8"))
    scores = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert {k: counts[k] for k in ("tp", "fp", "fn")} == {k: scores["validation"][k] for k in ("tp", "fp", "fn")}
    assert counts["split"] == "validation" and counts["threshold"] == scores["validation"]["threshold"]
    assert all(isinstance(counts[k], int) for k in ("tp", "fp", "fn"))
    assert scores["test"]["threshold"] == counts["threshold"]  # test reuses the validation threshold


def test_evaluate_recomputes_the_metrics_and_prints_the_gates_block(run_dir, capsys):
    assert evaluate.main(["--run", str(run_dir), "--gates-block"]) == 0
    block = json.loads(capsys.readouterr().out)
    scores = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert block == {k: scores["validation"][k] for k in ("tp", "fp", "fn")}


@pytest.mark.parametrize("split, key", [("validation", "tp"), ("validation", "pr_auc"), ("test", "brier_skill"),
                                        ("test", "log_loss")])
def test_evaluate_catches_metrics_that_disagree_with_the_predictions(run_dir, capsys, split, key):
    path = run_dir / "metrics.json"
    scores = json.loads(path.read_text(encoding="utf-8"))
    scores[split][key] += 1
    path.write_text(json.dumps(scores), encoding="utf-8")
    assert evaluate.main(["--run", str(run_dir)]) == 2
    assert f"{split} {key}" in capsys.readouterr().err


def test_metrics_json_holds_every_metric_for_both_splits(run_dir):
    scores = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    for split in ("validation", "test"):
        assert {"surge_rate", "auc", "pr_auc", "log_loss", "brier", "brier_skill"} <= set(scores[split])
        assert 0 < scores[split]["pr_auc"] <= 1 and scores[split]["log_loss"] > 0


def test_an_unknown_model_is_refused_with_the_known_ones(tmp_path, capsys):
    config = H.write_repo(tmp_path)
    assert train.main(["--model", "random_forest", "--config", str(config)]) == 2
    known = "['lightgbm', 'logreg', 'seasonal', 'xgboost']"
    assert f"unknown model 'random_forest'; known models: {known}" in capsys.readouterr().err
    assert sorted(models.MODELS) == ["lightgbm", "logreg", "seasonal", "xgboost"]


def test_a_logreg_run_records_what_it_chose_on_the_inner_folds(tmp_path, capsys):
    config = H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01"))
    assert train.main(["--model", "logreg", "--config", str(config), "--run-id", "lr"]) == 0
    out = capsys.readouterr().out
    run_dir = tmp_path / "runs" / "lr"
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    details = run["model_details"]
    grid = [c for c, _ in details["inner_log_loss_by_C"]]
    assert len(grid) == 11 and details["chosen"]["C"] in grid and details["converged"] is True
    assert [fold["year"] for fold in details["inner_folds"]] == [2019, 2022, 2023]
    assert f"chosen on the inner folds 2019, 2022, 2023: C {details['chosen']['C']:g}" in out
    assert "parameters: the starting values in model_params.yaml\n" in out and run["parameters"]["tuned"] == []
    assert set(P.read_predictions(run_dir / "predictions.parquet", threshold=run["threshold"])["model"]) == {"logreg"}
    assert evaluate.main(["--run", str(run_dir)]) == 0


def test_a_run_folder_is_never_overwritten(run_dir, tmp_path, capsys):
    config = tmp_path / "training.csv"
    assert train.main(["--model", "seasonal", "--config", str(config), "--run-id", "r1"]) == 2
    assert "already exists" in capsys.readouterr().err


def test_a_validation_year_with_no_surge_is_refused(tmp_path, capsys):
    labels = H.make_labels()
    labels.loc[labels["date"].dt.year == 2024, "surge"] = False
    config = H.write_repo(tmp_path, label_frame=labels)
    assert train.main(["--model", "seasonal", "--config", str(config)]) == 2
    assert "no surge in these labels" in capsys.readouterr().err


def test_a_bad_config_is_refused_before_anything_runs(tmp_path, capsys):
    config = H.write_repo(tmp_path, threshold_rule="fixed_half")
    assert train.main(["--model", "seasonal", "--config", str(config)]) == 2
    assert "threshold_rule must be one of" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("model", ["xgboost", "lightgbm"])
def test_a_tree_model_run_records_the_trees_it_chose_on_the_inner_folds(tmp_path, capsys, model):
    config = H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01"))
    assert train.main(["--model", model, "--config", str(config), "--run-id", "trees"]) == 0
    out = capsys.readouterr().out
    run_dir = tmp_path / "runs" / "trees"
    details = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["model_details"]
    trees = details["chosen"]["trees"]
    assert 1 <= trees and details["trees_grown_in_search"] == trees + 100 and details["stopped_before_max_rounds"]
    assert f"chosen on the inner folds 2019, 2022, 2023: trees {trees}" in out and "reached max_rounds" not in out
    assert evaluate.main(["--run", str(run_dir)]) == 0


@pytest.mark.parametrize("model", ["xgboost", "lightgbm"])
def test_a_tree_search_that_reaches_max_rounds_is_flagged_in_the_output(tmp_path, capsys, model):
    params = (H.training_data.ROOT / "Model-Development" / "config" / "model_params.yaml").read_text(encoding="utf-8")
    capped = tmp_path / "model_params.yaml"
    capped.write_text(params.replace("max_rounds: 3000", "max_rounds: 3"), encoding="utf-8")
    config = H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01"), model_params=str(capped))
    assert train.main(["--model", model, "--config", str(config), "--run-id", "trees"]) == 0
    assert "the tree search reached max_rounds without stopping" in capsys.readouterr().out


def test_a_run_records_and_prints_the_tuned_values_it_used(tmp_path, capsys):
    config = H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01"))
    (tmp_path / "tuned_params.yaml").write_text("logreg:\n  C_grid: [0.1]\n", encoding="utf-8")
    assert train.main(["--model", "logreg", "--config", str(config), "--run-id", "tuned"]) == 0
    out = capsys.readouterr().out
    run = json.loads((tmp_path / "runs" / "tuned" / "run.json").read_text(encoding="utf-8"))
    assert run["parameters"]["tuned"] == ["C_grid"] and run["parameters"]["values"]["C_grid"] == [0.1]
    assert run["model_details"]["chosen"] == {"C": 0.1}
    assert f"parameters: C_grid from tuned_params.yaml ({run['parameters']['tuned_sha256'][:19]})" in out
