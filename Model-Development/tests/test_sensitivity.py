"""Sensitivity analysis (#113): SHAP values of the selected model and the importance of M6's tuning
settings, on small fixtures; the smoke test runs the whole command on a temp repo, as in the M9 image."""

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

pytest.importorskip("shap")
optuna = pytest.importorskip("optuna")
import sensitivity as S  # noqa: E402
import tracking  # noqa: E402
import train  # noqa: E402
import training_data  # noqa: E402
import tune  # noqa: E402

INPUTS = ["a", "b", "c"]


def toy(n: int = 300, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    """Rows where a drives the label, c a little and b not at all, with b sometimes missing."""
    rng = np.random.default_rng(seed)
    rows = pd.DataFrame({name: rng.normal(size=n) for name in INPUTS})
    rows.loc[::9, "b"] = np.nan
    label = ((rows["a"] + 0.4 * rows["c"] + rng.normal(0, 0.6, n)) > 0.8).astype(int).to_numpy()
    rows["unit"] = np.where(np.arange(n) % 2, "Alpha", "Beta")
    rows["date"] = pd.Timestamp("2025-01-01") + pd.to_timedelta(np.arange(n) // 2, unit="D")
    return rows, label


def saved_model(library: str):
    rows, label = toy()
    if library == "lightgbm":
        import lightgbm as lgb
        estimator = lgb.LGBMClassifier(n_estimators=25, verbose=-1, random_state=0)
    elif library == "xgboost":
        import xgboost as xgb
        estimator = xgb.XGBClassifier(n_estimators=25, random_state=0)
    else:
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        estimator = make_pipeline(SimpleImputer(), LogisticRegression())
    estimator.fit(rows[INPUTS], label)
    return train.with_threshold(estimator, 0.5, rows[INPUTS], label), rows, label


# SHAP values


@pytest.mark.parametrize("library", ["lightgbm", "xgboost"])
def test_a_tree_models_values_add_up_to_its_log_odds_on_every_row(library):
    saved, rows, _ = saved_model(library)
    explanation, explained, method = S.explain(saved, rows, INPUTS, background=rows)
    assert len(explained) == len(rows) and method.startswith("TreeExplainer")
    assert S.add_up_gap(explanation, saved.predict_proba(rows[INPUTS])[:, 1]) < 1e-6
    table = S.importance_table(explanation, INPUTS)
    assert table["input"].iloc[0] == "a" and table["direction_rank_correlation"].iloc[0] > 0.3  # a pushes up
    assert table["rank"].tolist() == [1, 2, 3] and abs(table["share"].sum() - 1) < 1e-12
    assert table["mean_abs_shap"].is_monotonic_decreasing
    assert table.set_index("input").loc["b", "missing_share"] == pytest.approx(34 / 300)


def test_a_model_without_trees_is_explained_on_a_seeded_sample():
    saved, rows, _ = saved_model("logreg")
    first, picked, method = S.explain(saved, rows, INPUTS, background=rows, sample_rows=40)
    again, picked_again, _ = S.explain(saved, rows, INPUTS, background=rows, sample_rows=40)
    assert len(picked) == 40 and method.startswith("PermutationExplainer")
    assert picked.index.equals(picked_again.index) and np.array_equal(first.values, again.values)
    assert S.add_up_gap(first, saved.predict_proba(picked[INPUTS])[:, 1]) < 1e-6
    assert S.importance_table(first, INPUTS)["input"].iloc[0] == "a"


def test_the_saved_models_estimator_is_found_inside_its_threshold_and_freeze():
    saved, _, _ = saved_model("lightgbm")
    assert type(S.fitted_model(saved)).__name__ == "LGBMClassifier" and S.is_tree_model(S.fitted_model(saved))
    assert not S.is_tree_model(S.fitted_model(saved_model("logreg")[0]))


def test_direction_names_the_sign_only_when_the_rank_correlation_is_clear():
    assert S.direction(0.5) == "higher value, higher forecast"
    assert S.direction(-0.5) == "higher value, lower forecast"
    assert S.direction(0.1) == "mixed" and S.direction(float("nan")) == "no direction (constant)"


def test_the_examples_are_the_surest_of_each_kind_with_ties_to_the_earlier_day():
    rows = pd.DataFrame({
        "unit": ["A", "B", "A", "B", "A", "B"],
        "date": pd.to_datetime(["2025-01-02", "2025-01-01", "2025-01-01", "2025-01-03", "2025-01-04", "2025-01-05"]),
        "probability": [0.9, 0.9, 0.8, 0.7, 0.01, 0.2],
        "prediction": [1, 1, 1, 1, 0, 0],
        "label": [1, 1, 0, 0, 1, 1]})
    assert S.examples(rows) == {"true_alarm": 1, "false_alarm": 2, "missed_surge": 4}
    assert set(S.examples(rows[rows["label"] == 1])) == {"true_alarm", "missed_surge"}  # no false alarm to show


def test_the_explained_model_is_the_winner_or_else_the_best_candidate():
    ranking = [{"model": "lightgbm", "run_folder": "l"}, {"model": "xgboost", "run_folder": "x"}]
    assert S.selected({"winner": "xgboost", "ranking": ranking}) == (ranking[1], True)
    assert S.selected({"winner": None, "ranking": ranking}) == (ranking[0], False)
    with pytest.raises(S.SensitivityError, match="ranks no candidate"):
        S.selected({"winner": None, "ranking": []})


# M6's studies


def write_study(folder: Path, name: str = "lightgbm", trials: int = 30) -> optuna.Study:
    """A tune.py study file of name in folder, where the fold F1 follows colsample_bytree almost alone."""
    study = optuna.create_study(direction="maximize", study_name=f"tune-{name}",
                                sampler=optuna.samplers.RandomSampler(seed=0))
    for _ in range(trials):
        trial = study.ask(tune.SPACES[name])
        trial.set_user_attr("scores", {"log_loss": 0.2 - 0.05 * trial.params["colsample_bytree"]})
        study.tell(trial, trial.params["colsample_bytree"] + 0.001 * trial.params["learning_rate"])
    folder.mkdir(parents=True, exist_ok=True)
    record = {"model": name, "study_name": study.study_name, "direction": "maximize",
              "distributions": {key: optuna.distributions.distribution_to_json(value)
                                for key, value in tune.SPACES[name].items()},
              "trials": [{"number": t.number, "state": t.state.name, "params": t.params, "value": t.value,
                          "user_attrs": t.user_attrs} for t in study.trials]}
    (folder / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
    return study


def write_tuning(tmp_path: Path, **changes: str) -> Path:
    """A tuning.csv with the committed settings, study_dir in tmp_path, and changes by name."""
    with open(tune.DEFAULT_TUNING, encoding="utf-8", newline="") as handle:
        settings = {row["setting"]: row["value"] for row in csv.DictReader(handle)}
    settings.update(study_dir=str(tmp_path / "tuning"), **changes)
    path = tmp_path / "tuning.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["setting", "value", "options", "note"])
        writer.writerows([name, value, "", ""] for name, value in settings.items())
    return path


def test_the_studies_give_each_settings_importance_and_a_missing_study_is_only_noted(tmp_path):
    tuning = tune.load_tuning(write_tuning(tmp_path, models="logreg;lightgbm"))
    study = write_study(tuning.study_dir)
    shares = tune.importances(study, tuning.seed)
    with open(tuning.study_dir / "importance.csv", "w", encoding="utf-8", newline="") as handle:  # as tune.py writes it
        writer = csv.writer(handle)
        writer.writerow(["model", "setting", "importance_log_loss", "importance_f1"])
        writer.writerows(["lightgbm", key, round(share["log_loss"], 6), round(share["f1"], 6)] for key, share in shares.items())
    out = tmp_path / "out"
    out.mkdir()
    rows, summary, files = S.hyperparameters(tuning, out)
    assert summary["logreg"]["study"] is None and "run tune.py" in summary["logreg"]["note"]
    part = summary["lightgbm"]
    assert part["trials"] == 30 and part["top_settings"][0] == "colsample_bytree" and len(part["top_settings"]) == 2
    assert part["importance_csv"]["same_order"] and part["importance_csv"]["max_difference"] < 1e-6
    assert [row["setting"] for row in rows][:2] == part["top_settings"] and {row["model"] for row in rows} == {"lightgbm"}
    assert sorted(path.name for path in files) == ["hp_importance_lightgbm.png", "hp_slice_lightgbm.png"]
    assert all(path.stat().st_size > 0 for path in files)


# The command, end to end


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """A temp repo with a trained LightGBM run, the selection.json that names it and one M6 study."""
    root = tmp_path_factory.mktemp("sensitivity")
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv(tracking.ENV, raising=False)
        config = H.write_repo(root, label_frame=H.make_labels(first="2016-01-01"), reports_dir=str(root / "reports"))
        assert train.main(["--model", "lightgbm", "--config", str(config), "--run-id", "lightgbm-run"]) == 0
    settings = training_data.load_settings(config)
    run = json.loads((root / "runs" / "lightgbm-run" / "run.json").read_text(encoding="utf-8"))
    selection = {"selection_id": "s1", "winner": "lightgbm", "data_version": training_data.data_version(settings.dvc_lock),
                 "ranking": [{"model": "lightgbm", "run_folder": "lightgbm-run", "mlflow_run_id": run["mlflow"]["run_id"]}]}
    (root / "reports").mkdir()
    (root / "reports" / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
    tuning = write_tuning(root, models="lightgbm")
    write_study(root / "tuning")
    return root, config, tuning, settings, selection


def test_the_command_explains_the_selected_model_and_logs_one_mlflow_run(trained, capsys):
    root, config, tuning, settings, _ = trained
    assert S.main(["--config", str(config), "--tuning", str(tuning)]) == 0
    printed = capsys.readouterr().out
    out = root / "reports" / "sensitivity"
    assert "lightgbm: top inputs day_of_week" in printed and "experiment model-sensitivity" in printed
    names = {path.name for path in out.iterdir()}
    assert {"shap_importance.csv", "shap_bar.png", "shap_beeswarm.png", "shap_examples.csv", "hp_importance.csv",
            "hp_importance_lightgbm.png", "hp_slice_lightgbm.png", "sensitivity.json", "report.md"} <= names
    summary = json.loads((out / "sensitivity.json").read_text(encoding="utf-8"))
    test_days = training_data.load_table(settings)[0].query("split == 'test' and scored")
    assert summary["shap"]["rows"] == len(test_days) and summary["selected"] is True
    assert summary["checks"]["probability_max_difference"] <= 1e-9 and summary["checks"]["shap_add_up_max_gap"] <= 1e-6
    assert {f"shap_example_{kind}.png" for kind in (e["kind"] for e in summary["shap"]["examples"])} <= names
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "These are associations, not causes" in report and "| 1 | day_of_week |" in report

    import mlflow
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking.local_uri(settings.mlflow_store))
    experiment = client.get_experiment_by_name(S.EXPERIMENT)
    runs = client.search_runs([experiment.experiment_id], "tags.selection_id = 's1'")
    assert [run.info.run_name for run in runs] == ["sensitivity-lightgbm"]
    assert runs[0].data.tags["model_run_folder"] == "lightgbm-run" and runs[0].data.tags["top_input"] == "day_of_week"
    assert {"report.md", "shap_beeswarm.png", "hp_slice_lightgbm.png"} <= {a.path for a in client.list_artifacts(runs[0].info.run_id)}
    mlflow.set_tracking_uri(None)


def test_a_selection_from_another_data_version_stops_before_any_run(trained, tmp_path, capsys):
    root, config, tuning, _, selection = trained
    stale = tmp_path / "selection.json"
    stale.write_text(json.dumps({**selection, "data_version": "sha256:older"}), encoding="utf-8")
    assert S.main(["--config", str(config), "--tuning", str(tuning), "--selection", str(stale),
                   "--out", str(tmp_path / "out")]) == 2
    assert "comes from data version sha256:older" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_a_model_that_no_longer_gives_its_runs_probabilities_stops(trained, tmp_path, capsys):
    root, config, tuning, _, selection = trained
    copy = root / "runs" / "changed-run"
    copy.mkdir(exist_ok=True)
    for name in ("model.joblib", "run.json"):
        (copy / name).write_bytes((root / "runs" / "lightgbm-run" / name).read_bytes())
    frame = pd.read_parquet(root / "runs" / "lightgbm-run" / "predictions.parquet")
    frame.loc[frame["split"] == "test", "probability"] += 0.01
    frame.to_parquet(copy / "predictions.parquet", index=False)
    changed = tmp_path / "selection.json"
    changed.write_text(json.dumps({**selection, "ranking": [{"model": "lightgbm", "run_folder": "changed-run"}]}),
                       encoding="utf-8")
    assert S.main(["--config", str(config), "--tuning", str(tuning), "--selection", str(changed),
                   "--out", str(tmp_path / "out")]) == 2
    assert "away from its predictions.parquet" in capsys.readouterr().err
