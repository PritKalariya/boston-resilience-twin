"""Tests for what a training run logs to MLflow (#107, mlflow_runs.py): what a run logs, where, and the saved model."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
from training_helpers import no_mlflow_server, shared_mlflow_store  # noqa: E402,F401  (autouse: MLflow stays in temp folders)
import mlflow_runs  # noqa: E402  (sets the MLflow switches before mlflow loads)
import mlflow  # noqa: E402
import mlflow.sklearn  # noqa: E402
import train  # noqa: E402
import training_data as D  # noqa: E402


def _run(tmp_path, model, labels=None, run_id="r", **changes):
    """Train model on a temp repo with labels from 2016, logging to the session's shared MLflow store
    (a new store costs seconds of database setup); returns the run folder, run.json and an MLflow
    client on that store. Each test reads back its own run by the id in run.json."""
    config = H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01") if labels is None else labels,
                          **changes)
    settings = D.load_settings(config)
    run_dir = train.train(model, settings, run_id)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return run_dir, run, mlflow.MlflowClient(mlflow_runs.tracking_uri(settings))


def test_a_candidate_run_logs_tags_params_metrics_files_and_its_model(tmp_path):
    run_dir, run, client = _run(tmp_path, "logreg")
    logged = client.get_run(run["mlflow"]["run_id"])
    assert logged.info.status == "FINISHED" and logged.info.run_name == "r"
    assert client.get_experiment(logged.info.experiment_id).name == "model-training"
    tags = logged.data.tags
    assert tags["data_version"] == run["data_version"] and tags["model"] == "logreg"
    assert tags["git_commit"] == str(run["git"]["commit"]) and tags["undefined_metrics"] == "none"
    assert tags["validation_days"] == "2024-01-01 to 2024-12-31"
    params = logged.data.params
    assert params["chosen_C"] == str(run["model_details"]["chosen"]["C"]) and params["inputs"] == "1"
    assert json.loads(params["param_C_grid"]) == list(run["parameters"]["values"]["C_grid"])
    assert params["inner_fold_years"] == "[2019, 2022, 2023]" and params["tuned"] == "none"
    scores = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    metrics = logged.data.metrics
    assert metrics["validation_f1"] == pytest.approx(scores["validation"]["f1"])
    assert metrics["test_pr_auc"] == pytest.approx(scores["test"]["pr_auc"])
    assert metrics["model_size_bytes"] == (run_dir / "model.joblib").stat().st_size > 0
    assert {"train_time_s", "score_time_evening_ms_median", "score_time_evening_ms_p95", "inner_log_loss"} <= set(metrics)
    files = {item.path for item in client.list_artifacts(logged.info.run_id)}
    assert files == {"inputs.json", "metrics.json", "predictions.parquet", "run.json", "validation_counts.json"}
    assert run["mlflow"]["where"].startswith("local store") and run["mlflow"]["model_uri"].startswith("models:/")


def test_the_saved_model_carries_its_threshold_and_reproduces_the_run(tmp_path):
    run_dir, run, _ = _run(tmp_path, "lightgbm")
    frame = pd.read_parquet(run_dir / "predictions.parquet")
    table, _ = D.load_table(D.load_settings(tmp_path / "training.csv"))
    rows = frame.merge(table[["unit", "date", *run["inputs"]]], on=["unit", "date"])
    for model in (joblib.load(run_dir / "model.joblib"), mlflow.sklearn.load_model(run["mlflow"]["model_uri"])):
        assert model.threshold == run["threshold"]
        np.testing.assert_allclose(model.predict_proba(rows[run["inputs"]])[:, 1], rows["probability"], rtol=0, atol=1e-12)
        np.testing.assert_array_equal(model.predict(rows[run["inputs"]]), rows["prediction"])


def test_the_costs_are_measured_on_one_evening(tmp_path):
    _, run, _ = _run(tmp_path, "xgboost")
    costs = run["costs"]
    assert costs["train_time_s"] > 0 and costs["model_size_bytes"] > 0
    evening = costs["score_time_evening_ms"]
    assert evening["rows"] == 2  # the 2 units of one day
    assert train.EVENING_MIN_REPEATS <= evening["repeats"] <= train.EVENING_REPEATS
    assert 0 < evening["median"] <= evening["p95"]


class _Clock:
    """A model whose every scoring takes step_s seconds of a fake clock."""

    def __init__(self, monkeypatch, step_s):
        self.now, self.step_s = 0.0, step_s
        monkeypatch.setattr(train.time, "perf_counter", lambda: self.now)

    def predict_proba(self, rows):
        self.now += self.step_s


@pytest.mark.parametrize("step_s, tries", [(0.001, 50), (0.125, 8), (0.5, 5)])  # steps exact in binary
def test_scoring_stops_at_the_repeats_or_after_the_budget_with_the_minimum(monkeypatch, step_s, tries):
    timed = train.scoring_ms(_Clock(monkeypatch, step_s), pd.DataFrame({"unit": ["a"]}))
    assert timed["repeats"] == tries and timed["median"] == pytest.approx(step_s * 1000)


def test_the_baseline_is_logged_without_a_model(tmp_path):
    run_dir, run, client = _run(tmp_path, "seasonal")
    logged = client.get_run(run["mlflow"]["run_id"])
    assert run["mlflow"]["model_uri"] is None and run["costs"]["model_size_bytes"] is None
    assert not (run_dir / "model.joblib").exists() and "model_size_bytes" not in logged.data.metrics
    assert logged.data.params["seasonal_years"] == "3" and not any(p.startswith("param_") for p in logged.data.params)


def test_an_undefined_metric_is_left_out_and_named_in_a_tag(tmp_path):
    labels = H.make_labels(first="2016-01-01")
    labels.loc[labels["date"] >= "2025-01-01", "surge"] = False  # no surge in the test window
    _, run, client = _run(tmp_path, "seasonal", labels=labels)
    logged = client.get_run(run["mlflow"]["run_id"])
    undefined = logged.data.tags["undefined_metrics"].split(",")
    assert {"test_recall", "test_auc", "test_pr_auc", "test_brier_skill"} <= set(undefined)
    assert not set(undefined) & set(logged.data.metrics) and "test_rows" in logged.data.metrics


def test_a_failed_run_is_marked_failed_in_mlflow(tmp_path, capsys):
    labels = H.make_labels(first="2016-01-01")
    labels.loc[labels["date"].dt.year == 2024, "surge"] = False  # validation cannot pick a threshold
    config = H.write_repo(tmp_path, label_frame=labels)
    assert train.main(["--model", "logreg", "--config", str(config), "--run-id", "failed-run"]) == 2
    assert "no surge in these labels" in capsys.readouterr().err
    client = mlflow.MlflowClient(mlflow_runs.tracking_uri(D.load_settings(config)))
    experiment = client.get_experiment_by_name("model-training")
    runs = client.search_runs([experiment.experiment_id], filter_string="attributes.run_name = 'failed-run'")
    assert [r.info.status for r in runs] == ["FAILED"]
    assert not (tmp_path / "runs" / "failed-run").exists()  # no run folder for a run that failed


def test_a_run_inside_another_run_is_logged_as_its_child(tmp_path):
    config = H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01"))
    settings = D.load_settings(config)
    with mlflow_runs.tracked_run(settings, "selection", {"purpose": "test"}) as parent:
        run_dir = train.train("seasonal", settings, "child")
    child = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["mlflow"]["run_id"]
    logged = mlflow.MlflowClient(mlflow_runs.tracking_uri(settings)).get_run(child)
    assert logged.data.tags["mlflow.parentRunId"] == parent.info.run_id


def test_a_local_run_leaves_no_server_variable_and_no_stray_store(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # MLflow's default store would land here, as ./mlruns
    _run(tmp_path, "logreg")
    assert "MLFLOW_TRACKING_URI" not in os.environ  # set_tracking_uri writes it; a local run removes it
    assert not (elsewhere / "mlruns").exists() and (H.MLFLOW_STORE / "artifacts").is_dir()


def test_a_server_named_in_the_environment_is_used_and_its_address_not_written_down(tmp_path, monkeypatch):
    server = tmp_path / "server"
    server.mkdir()
    uri = f"sqlite:///{(server / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    monkeypatch.chdir(server)  # a server keeps the artifacts; this stand-in puts them in ./mlruns, here
    _, run, _ = _run(tmp_path, "seasonal", mlflow_store=str(tmp_path / "local"))
    assert run["mlflow"]["where"] == "server (MLFLOW_TRACKING_URI)" and uri not in json.dumps(run)
    assert not (tmp_path / "local").exists()  # the local store was not used
    assert mlflow.MlflowClient(uri).get_run(run["mlflow"]["run_id"]).info.status == "FINISHED"


def test_params_flatten_lists_and_metrics_skip_undefined_values():
    run = {"model": "logreg", "threshold": 0.25, "threshold_rule": "validation_f1_max", "inputs": ["a", "b"],
           "model_details": {"chosen": {"C": 0.1}, "inner_log_loss": 0.12},
           "parameters": {"values": {"C_grid": (0.01, 0.1)}, "tuned": ["C_grid"], "tuned_sha256": "sha256:x"},
           "settings": {"inner_fold_years": [2019], "down_weight_years": [2020], "down_weight": 0.5,
                        "missing_gap_max_points": 10.0},
           "costs": {"train_time_s": 1.5, "score_time_evening_ms": {"median": 2.0, "p95": 3.0}, "model_size_bytes": 99}}
    params = mlflow_runs.params(run)
    assert params["param_C_grid"] == "[0.01, 0.1]" and params["tuned"] == "C_grid" and params["chosen_C"] == "0.1"
    values, undefined = mlflow_runs.metrics(run, {"test": {"f1": 0.5, "recall": None}})
    assert values == {"test_f1": 0.5, "inner_log_loss": 0.12, "train_time_s": 1.5, "score_time_evening_ms_median": 2.0,
                      "score_time_evening_ms_p95": 3.0, "model_size_bytes": 99.0}
    assert undefined == ["test_recall"]
