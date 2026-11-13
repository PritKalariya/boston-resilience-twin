"""Tests for the comparison of tuned values with the starting ones on validation (#109,
compare_tuned.py): the paired bootstrap, the two selections it asks M4's selection.py for,
comparison.json with its MLflow run, and the command line. selection.run_selection is replaced by a
stand-in that writes prediction files, so no model is trained here."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
from training_helpers import no_mlflow_server, shared_mlflow_store  # noqa: E402,F401  (autouse: MLflow stays in temp folders)
import mlflow_runs  # noqa: E402  (sets the MLflow switches before mlflow loads)
import mlflow  # noqa: E402
import compare_tuned  # noqa: E402
import training_data as D  # noqa: E402
import tune  # noqa: E402

QUIET = {"log": lambda *args, **kwargs: None}
CUT = 0.5


def predictions_frame(model: str, seed: int, days: int = 30, units: int = 4) -> pd.DataFrame:
    """Validation predictions in the prediction contract's columns: the same labels for every seed,
    probabilities that depend on the seed, flagged at CUT."""
    n = days * units
    label = (np.random.default_rng(0).random(n) < 0.3).astype("int64")
    probability = np.clip(0.4 * label + 0.6 * np.random.default_rng(seed).random(n), 0.0, 1.0)
    return pd.DataFrame({"unit": np.tile([f"u{i}" for i in range(units)], days),
                         "date": np.repeat(pd.date_range("2024-01-01", periods=days), units),
                         "model": model, "split": "validation", "probability": probability,
                         "prediction": (probability >= CUT).astype("int64"), "label": label})


def test_identical_runs_gain_nothing_on_any_draw():
    frame = predictions_frame("lightgbm", 1)
    boot = compare_tuned.paired_bootstrap(frame, frame.copy(), draws=200, seed=0)
    assert boot["f1_gain_ci95"] == [0.0, 0.0] and boot["log_loss_gain_ci95"] == [0.0, 0.0]
    assert boot["f1_tuned_higher_share"] == 0.0 and boot["log_loss_tuned_lower_share"] == 0.0
    assert boot["days"] == 30 and boot["rows"] == 120 and boot["draws"] == 200


def test_a_run_right_on_every_row_wins_every_draw():
    default = predictions_frame("lightgbm", 1)
    tuned = default.copy()
    tuned["prediction"] = tuned["label"]
    tuned["probability"] = np.where(tuned["label"] == 1, 0.99, 0.01)
    boot = compare_tuned.paired_bootstrap(default, tuned, draws=200, seed=0)
    assert boot["f1_gain_ci95"][0] > 0 and boot["log_loss_gain_ci95"][1] < 0
    assert boot["f1_tuned_higher_share"] == 1.0 and boot["log_loss_tuned_lower_share"] == 1.0


def test_the_same_seed_draws_the_same_days_and_other_rows_are_refused():
    a, b = predictions_frame("lightgbm", 1), predictions_frame("lightgbm", 2)
    assert compare_tuned.paired_bootstrap(a, b, 100, 7) == compare_tuned.paired_bootstrap(a, b, 100, 7)
    assert compare_tuned.paired_bootstrap(a, b, 100, 7) != compare_tuned.paired_bootstrap(a, b, 100, 8)
    with pytest.raises(ValueError, match="different validation rows"):
        compare_tuned.paired_bootstrap(a, predictions_frame("lightgbm", 1, days=29), 100, 0)


def write_tuning(tmp_path: Path) -> Path:
    """A tuning.csv in tmp_path with the committed settings and study_dir in tmp_path."""
    with open(tune.DEFAULT_TUNING, encoding="utf-8", newline="") as handle:
        settings = {row["setting"]: row["value"] for row in csv.DictReader(handle)}
    settings.update(study_dir=str(tmp_path / "tuning"))
    path = tmp_path / "tuning.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["setting", "value", "options", "note"])
        writer.writerows([name, value, "", ""] for name, value in settings.items())
    return path


@pytest.fixture
def comparing(tmp_path, monkeypatch):
    """A temp repo, its tuning settings, a candidate file tuning LightGBM, and selection.run_selection
    replaced by a stand-in. The stand-in writes a prediction file for the seasonal baseline and
    LightGBM (another LightGBM when the selection's tuned file exists) and records what it was given."""
    config = H.write_repo(tmp_path)
    tuning_path = write_tuning(tmp_path)
    settings, tuning = D.load_settings(config), tune.load_tuning(tuning_path)
    candidate = tuning.study_dir / tune.BEST_PARAMS
    tune.write_tuned(candidate, {"lightgbm": {"num_leaves": 19}}, ["a test candidate"])
    calls = []

    def run_selection(kind_settings, selection_id, reports_dir, progress=print):
        tuned = kind_settings.tuned_params.exists()
        calls.append({"tuned_params": kind_settings.tuned_params, "tuned": tuned, "selection_id": selection_id,
                      "reports_dir": Path(reports_dir), "experiment": kind_settings.mlflow_experiment})
        records = {}
        for model, seed in (("seasonal", 5), ("lightgbm", 2 if tuned else 1)):
            folder = f"{selection_id}-{model}"
            (kind_settings.runs_dir / folder).mkdir(parents=True)
            frame = predictions_frame(model, seed)
            frame.to_parquet(kind_settings.runs_dir / folder / "predictions.parquet", index=False)
            label, flag = frame["label"].to_numpy(), frame["prediction"].to_numpy()
            records[model] = {"model": model, "threshold": CUT, "chosen": {}, "run_folder": folder, "validation": {
                "f1": compare_tuned._f1(flag, label), "precision": 0.5, "recall": 0.5,
                "log_loss": compare_tuned._log_loss(frame["probability"].to_numpy(), label), "auc": None,
                "pr_auc": 0.4, "brier_skill": 0.1, "tp": 1, "fp": 1, "fn": 1, "rows": len(frame),
                "surges": int(label.sum())}}
        return {"selection_id": selection_id, "winner": "lightgbm", "data_version": D.data_version(kind_settings.dvc_lock),
                "baseline": records["seasonal"], "ranking": [records["lightgbm"]]}, Path(reports_dir)

    monkeypatch.setattr(compare_tuned.selection, "run_selection", run_selection)
    return settings, tuning, candidate, calls, config, tuning_path


def test_the_comparison_runs_m4s_selection_with_the_starting_values_and_with_the_candidate(comparing):
    settings, tuning, candidate, calls, _, _ = comparing
    result = compare_tuned.compare(settings, tuning, candidate, "compare-test-runs", **QUIET)
    default, tuned = calls
    assert not default["tuned"] and default["tuned_params"].parent == settings.runs_dir
    assert tuned["tuned"] and tuned["tuned_params"] == candidate
    assert default["experiment"] == tuned["experiment"] == tuning.mlflow_experiment
    assert [call["selection_id"] for call in calls] == ["compare-test-runs-default", "compare-test-runs-tuned"]
    assert all(call["reports_dir"].parent == settings.runs_dir for call in calls)  # never reports_dir
    assert not settings.tuned_params.exists()  # neither read nor written

    written = json.loads((tuning.study_dir / "comparison.json").read_text(encoding="utf-8"))
    assert written == json.loads(json.dumps(result))
    assert written["selections"] == {"default": "compare-test-runs-default", "tuned": "compare-test-runs-tuned"}
    assert written["data_version"] == D.data_version(settings.dvc_lock) and written["split"] == "validation"
    lightgbm, seasonal = written["models"]["lightgbm"], written["models"]["seasonal"]
    assert lightgbm["tuned_values"] == {"num_leaves": 19} and "bootstrap" not in seasonal
    assert lightgbm["difference"]["f1"] == pytest.approx(lightgbm["tuned"]["f1"] - lightgbm["default"]["f1"])
    assert "auc" not in lightgbm["difference"]  # a metric a run left undefined has no difference
    assert seasonal["difference"]["f1"] == 0.0
    boot = lightgbm["bootstrap"]
    assert boot["draws"] == settings.bootstrap_resamples and boot["seed"] == settings.bootstrap_seed and boot["days"] == 30
    assert boot["f1_gain_ci95"][0] <= lightgbm["difference"]["f1"] <= boot["f1_gain_ci95"][1]
    assert 0 < lightgbm["same_call_share"] < 1


def test_the_comparison_is_an_mlflow_run_with_both_selections_and_the_gain(comparing):
    settings, tuning, candidate, _, _, _ = comparing
    compare_tuned.compare(settings, tuning, candidate, "compare-test-mlflow", **QUIET)
    client = mlflow.MlflowClient(mlflow_runs.tracking_uri(settings))
    experiment = client.get_experiment_by_name(tuning.mlflow_experiment)
    run, = client.search_runs([experiment.experiment_id], "attributes.run_name = 'compare-test-mlflow'")
    assert {"lightgbm_default_f1", "lightgbm_tuned_f1", "lightgbm_f1_gain", "lightgbm_f1_gain_ci95_low",
            "lightgbm_log_loss_gain_ci95_high", "lightgbm_same_call_share", "seasonal_tuned_f1"} <= set(run.data.metrics)
    assert run.data.tags["winner_default"] == run.data.tags["winner_tuned"] == "lightgbm"
    assert run.data.params["tuned_models"] == '["lightgbm"]'
    assert {item.path for item in client.list_artifacts(run.info.run_id)} == {"comparison.json"}


def test_a_candidate_file_that_tunes_nothing_or_a_used_name_is_refused(comparing, tmp_path):
    settings, tuning, candidate, _, _, _ = comparing
    empty = tmp_path / "empty.yaml"
    tune.write_tuned(empty, {}, ["nothing"])
    with pytest.raises(ValueError, match="tunes no model"):
        compare_tuned.compare(settings, tuning, empty, "compare-test-empty", **QUIET)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    (settings.runs_dir / "compare-test-taken-no-tuned-values.yaml").write_text("lightgbm: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="would not run alone"):
        compare_tuned.compare(settings, tuning, candidate, "compare-test-taken", **QUIET)


def test_main_prints_the_table_and_exits_0(comparing, capsys):
    _, tuning, _, _, config, tuning_path = comparing
    assert compare_tuned.main(["--config", str(config), "--tuning", str(tuning_path), "--id", "compare-test-main"]) == 0
    out = capsys.readouterr().out
    assert "comparison compare-test-main" in out and "(not tuned)" in out
    assert (tuning.study_dir / "comparison.json").exists()


def test_main_names_a_missing_candidate_and_exits_2(comparing, capsys, tmp_path):
    _, _, _, calls, config, tuning_path = comparing
    assert compare_tuned.main(["--config", str(config), "--tuning", str(tuning_path),
                               "--candidate", str(tmp_path / "missing.yaml")]) == 2
    assert "does not exist: run tune.py first" in capsys.readouterr().err
    assert not calls
