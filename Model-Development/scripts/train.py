"""Train one named model and write its run (#106, ADR-027).

    python Model-Development/scripts/train.py --model seasonal|logreg|xgboost|lightgbm [--config Model-Development/config/training.csv]

1. Loads the training table (training_data.load_table): the frozen labels and the model inputs of the
   data version Data-Pipeline/dvc.lock names, each labeled neighborhood-day in one split.
2. Fits the model on the train split and predicts the scored rows of the validation and test splits
   (a day with no day-before forecast is not scored; training_data.py). A candidate (#107) first
   picks its own settings on the inner folds of the train years (inner_folds.py).
3. Picks the threshold on validation with the shared rule (threshold.py) and applies the same
   threshold to test, which never picks it.
4. Writes Model-Development/runs/<run id>/ (runs_dir in the config; gitignored):
   - predictions.parquet: the predictions.py contract, the scored validation and test rows;
   - metrics.json, per split: the counts at the threshold with the F1, precision and recall they
     give, the surge rate, AUC, PR-AUC, log loss, Brier score and Brier skill (threshold.metrics);
   - validation_counts.json: tp, fp and fn on validation with the data version, the block the
     promotion gates read for a candidate or for the baseline (ADR-026);
   - run.json: the model, threshold, data version, git commit, model inputs, settings, row counts,
     a candidate's parameters (the values used, the ones tuned_params.yaml set and that file's
     sha256), the model's details (what it chose on the inner folds and how each choice scored),
     the costs (training time, the time to score one evening's rows, the saved model's size) and
     where MLflow holds the run;
   - model.joblib, for a candidate: the fitted model with its threshold (with_threshold), which loads
     with scikit-learn and the model's library alone, no code from this repo.
5. Logs the run to MLflow (mlflow_runs.py): tags, params, metrics, the run's files and, for a
   candidate, the model. A failure on the way marks the MLflow run FAILED.

Every setting comes from the config file; nothing needs a code edit. Exits 0 when the run is written
and 2, with the reason, on any error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.frozen import FrozenEstimator
from sklearn.model_selection import FixedThresholdClassifier

import models
import predictions
import threshold
import mlflow_runs
import training_data

PREDICTED_SPLITS = ("validation", "test")
# Scoring one evening's rows is timed up to EVENING_REPEATS times, stopping early once
# EVENING_BUDGET_S has passed with at least EVENING_MIN_REPEATS timings: a fast model gets all
# 50, the seasonal baseline (tens of milliseconds a try) enough for a steady median.
EVENING_REPEATS, EVENING_MIN_REPEATS, EVENING_BUDGET_S = 50, 5, 1.0


def git_state(root: Path) -> dict:
    """The checked-out commit and whether the tree has uncommitted changes; None where git cannot tell."""
    def run(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip()
    commit, status = run("rev-parse", "HEAD"), run("status", "--porcelain")
    return {"commit": commit or None, "uncommitted_changes": None if status is None else bool(status)}


def _settings_record(settings: training_data.Settings) -> dict:
    out = {}
    for key, value in asdict(settings).items():
        if key == "splits":
            out[key] = {split: [str(first.date()), str(last.date())] for split, (first, last) in value.items()}
        elif isinstance(value, Path):
            out[key] = value.as_posix()
        elif isinstance(value, tuple):
            out[key] = list(value)
        else:
            out[key] = value
    return out


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def scoring_ms(model, rows: pd.DataFrame, repeats: int = EVENING_REPEATS, minimum: int = EVENING_MIN_REPEATS,
               budget_s: float = EVENING_BUDGET_S) -> dict:
    """The model's time to score rows in milliseconds, the median and 95th percentile of up to repeats
    tries; it stops early once budget_s has passed with at least minimum tries. Reports how many."""
    times, started = [], time.perf_counter()
    while len(times) < repeats and (len(times) < minimum or time.perf_counter() - started < budget_s):
        start = time.perf_counter()
        model.predict_proba(rows)
        times.append((time.perf_counter() - start) * 1000)
    return {"median": round(float(np.median(times)), 3), "p95": round(float(np.percentile(times, 95)), 3),
            "rows": len(rows), "repeats": len(times)}


def with_threshold(estimator, cut: float, rows: pd.DataFrame, label) -> FixedThresholdClassifier:
    """The fitted estimator with the run's threshold attached, the model a run saves: predict gives the
    surge flag (probability >= cut) and predict_proba the probability. FrozenEstimator keeps fit from
    refitting it; fit only records the classes, here from rows and label."""
    return FixedThresholdClassifier(FrozenEstimator(estimator), threshold=cut).fit(rows, label)


def train(model_name: str, settings: training_data.Settings, run_id: str | None = None,
          runs_dir: str | Path | None = None) -> Path:
    """Train model_name under settings, write its run folder, log it to MLflow and return the folder."""
    table, counts = training_data.load_table(settings)
    version = training_data.data_version(settings.dvc_lock)
    git = git_state(training_data.ROOT)
    model = models.build(model_name, table, settings)
    created = datetime.now(timezone.utc)
    run_id = run_id or f"{created:%Y%m%dT%H%M%SZ}-{model_name}"
    run_dir = Path(runs_dir or settings.runs_dir) / run_id
    if run_dir.exists():
        raise ValueError(f"run folder {run_dir} already exists")
    tags = {"model": model_name, "data_version": version, "git_commit": git["commit"],
            "git_uncommitted_changes": git["uncommitted_changes"], "run_folder": run_id,
            **{f"{split}_days": f"{first.date()} to {last.date()}" for split, (first, last) in settings.splits.items()}}

    with mlflow_runs.tracked_run(settings, run_id, tags) as mlflow_run:  # a failure marks the MLflow run FAILED
        train_rows = table[table["split"] == "train"]
        start = time.perf_counter()
        model.fit(train_rows, train_rows["label"].to_numpy(), train_rows["weight"].to_numpy())
        train_time_s = time.perf_counter() - start
        rows, probability = {}, {}
        for split in PREDICTED_SPLITS:  # only the scored rows: a day with no forecast is not scored
            rows[split] = table[(table["split"] == split) & table["scored"]].reset_index(drop=True)
            if rows[split].empty:
                raise ValueError(f"the {split} split has no scored rows")
            probability[split] = np.asarray(model.predict_proba(rows[split]), dtype="float64")
            if len(probability[split]) != len(rows[split]):
                raise ValueError(f"{model_name} returned {len(probability[split])} probabilities for "
                                 f"{len(rows[split])} {split} rows")

        cut = threshold.shared_threshold(probability["validation"], rows["validation"]["label"],
                                         settings.threshold_rule)
        frame = pd.concat([predictions.make_predictions(rows[split], model_name, probability[split], cut)
                           for split in PREDICTED_SPLITS], ignore_index=True)
        scores = {split: threshold.metrics(probability[split], rows[split]["label"], cut)
                  for split in PREDICTED_SPLITS}

        run_dir.mkdir(parents=True)
        predictions.write_predictions(frame, run_dir / "predictions.parquet", cut)
        _write_json(run_dir / "metrics.json", scores)
        validation = scores["validation"]
        _write_json(run_dir / "validation_counts.json", {
            "data_version": version, "model": model_name, "split": "validation", "threshold": cut,
            "tp": validation["tp"], "fp": validation["fp"], "fn": validation["fn"]})

        # one evening is every neighborhood of one day: the last validation day
        evening = rows["validation"][rows["validation"]["date"] == rows["validation"]["date"].max()]
        costs = {"train_time_s": round(train_time_s, 3), "score_time_evening_ms": scoring_ms(model, evening),
                 "model_size_bytes": None}
        model_uri = None
        if getattr(model, "estimator", None) is not None:  # a candidate: save it with its threshold
            saved = with_threshold(model.estimator, cut, rows["validation"][model.inputs], rows["validation"]["label"])
            joblib.dump(saved, run_dir / "model.joblib")
            costs["model_size_bytes"] = (run_dir / "model.joblib").stat().st_size
            model_uri = mlflow_runs.log_model(saved, rows["validation"][model.inputs].head(5), model_name)

        run = {"run_id": run_id, "model": model_name, "created_utc": created.isoformat(timespec="seconds"),
               "threshold_rule": settings.threshold_rule, "threshold": cut, "data_version": version, "git": git,
               "rows": counts, "inputs": training_data.input_columns(table), "settings": _settings_record(settings),
               "parameters": getattr(model, "parameters", {}), "model_details": getattr(model, "details", {}),
               "costs": costs, "mlflow": {"run_id": mlflow_run.info.run_id, "experiment": settings.mlflow_experiment,
                                          "where": mlflow_runs.where(settings), "model_uri": model_uri}}
        _write_json(run_dir / "run.json", run)
        mlflow_runs.log_run(run_dir, run, scores)
    return run_dir


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train one named model and write its run (#106).")
    parser.add_argument("--model", required=True, help="a model name from models.MODELS, for example seasonal")
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG), help="training.csv path")
    parser.add_argument("--runs-dir", help="write the run here instead of the config's runs_dir")
    parser.add_argument("--run-id", help="the run folder's name (default: UTC time and model)")
    args = parser.parse_args(argv)
    try:
        settings = training_data.load_settings(args.config)
        run_dir = train(args.model, settings, args.run_id, args.runs_dir)
    except Exception as exc:  # a bad config, missing data or a failed check: report it and stop
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scores = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    print(f"run {run['run_id']}: model {run['model']}, data {run['data_version'][:19]}, commit {run['git']['commit']}")
    parameters = run["parameters"]
    starting, tuned = (Path(run["settings"][key]).name for key in ("model_params", "tuned_params"))
    if parameters.get("tuned"):
        print(f"parameters: {', '.join(parameters['tuned'])} from {tuned} ({parameters['tuned_sha256'][:19]}); "
              f"the rest from {starting}")
    elif parameters:
        print(f"parameters: the starting values in {starting}"
              + (f" ({tuned} sets none for this model)" if parameters["tuned_sha256"] else ""))
    details = run["model_details"]
    if details.get("chosen"):
        years = ", ".join(str(fold["year"]) for fold in details["inner_folds"])
        chosen = ", ".join(f"{name} {value:g}" for name, value in details["chosen"].items())
        print(f"chosen on the inner folds {years}: {chosen} ({details['chosen_by']})")
        if details.get("stopped_before_max_rounds") is False:
            print("note: the tree search reached max_rounds without stopping; raise max_rounds in model_params.yaml")
    for split in PREDICTED_SPLITS:
        note = training_data.unscored_note(run["rows"], split)
        if note:
            print(note)
    print(f"threshold {run['threshold']:.6f} ({run['threshold_rule']})")
    print(f"{'split':<11}{'rows':>7}{'surges':>8}{'tp':>6}{'fp':>7}{'fn':>6}{'f1':>9}{'precision':>11}{'recall':>9}"
          f"{'auc':>9}{'pr_auc':>9}{'log_loss':>10}{'brier_ss':>10}")
    for split in PREDICTED_SPLITS:
        s = scores[split]
        print(f"{split:<11}{s['rows']:>7}{s['surges']:>8}{s['tp']:>6}{s['fp']:>7}{s['fn']:>6}"
              f"{_fmt(s['f1']):>9}{_fmt(s['precision']):>11}{_fmt(s['recall']):>9}{_fmt(s['auc']):>9}"
              f"{_fmt(s['pr_auc']):>9}{_fmt(s['log_loss']):>10}{_fmt(s['brier_skill']):>10}")
    costs, evening = run["costs"], run["costs"]["score_time_evening_ms"]
    size = "" if costs["model_size_bytes"] is None else f"; saved model {costs['model_size_bytes']:,} bytes"
    print(f"costs: training {costs['train_time_s']:.1f} s; scoring one evening ({evening['rows']} rows) "
          f"{evening['median']:.1f} ms median, {evening['p95']:.1f} ms p95{size}")
    tracked = run["mlflow"]
    print(f"mlflow: run {tracked['run_id']} in experiment {tracked['experiment']} ({tracked['where']})"
          + (f"; model {tracked['model_uri']}" if tracked["model_uri"] else ""))
    print(f"wrote {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
