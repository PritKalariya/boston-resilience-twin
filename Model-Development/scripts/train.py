"""Train one named model and write its run (#106, ADR-027).

    python Model-Development/scripts/train.py --model seasonal|logreg|xgboost [--config Model-Development/config/training.csv]

1. Loads the training table (training_data.load_table): the frozen labels and the model inputs of the
   data version Data-Pipeline/dvc.lock names, each labeled neighborhood-day in one split.
2. Fits the model on the train split and predicts the validation and test splits. A candidate (#107)
   first picks its own settings on the inner folds of the train years (inner_folds.py).
3. Picks the threshold on validation with the shared rule (threshold.py) and applies the same
   threshold to test, which never picks it.
4. Writes Model-Development/runs/<run id>/ (runs_dir in the config; gitignored):
   - predictions.parquet: the predictions.py contract, the validation and test rows;
   - metrics.json: F1, precision, recall, AUC and the counts they come from, per split;
   - validation_counts.json: tp, fp and fn on validation with the data version, the block the
     promotion gates read for a candidate or for the baseline (ADR-026);
   - run.json: the model, threshold, data version, git commit, model inputs, settings, row counts and
     the model's details (what a candidate chose on the inner folds and how each choice scored).

Every setting comes from the config file; nothing needs a code edit. Exits 0 when the run is written
and 2, with the reason, on any error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import models
import predictions
import threshold
import training_data

PREDICTED_SPLITS = ("validation", "test")


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


def train(model_name: str, settings: training_data.Settings, run_id: str | None = None,
          runs_dir: str | Path | None = None) -> Path:
    """Train model_name under settings, write its run folder and return it."""
    table, counts = training_data.load_table(settings)
    version = training_data.data_version(settings.dvc_lock)
    model = models.build(model_name, table, settings)

    train_rows = table[table["split"] == "train"]
    model.fit(train_rows, train_rows["label"].to_numpy(), train_rows["weight"].to_numpy())
    rows, probability = {}, {}
    for split in PREDICTED_SPLITS:
        rows[split] = table[table["split"] == split].reset_index(drop=True)
        if rows[split].empty:
            raise ValueError(f"the {split} split has no labeled rows")
        probability[split] = np.asarray(model.predict_proba(rows[split]), dtype="float64")
        if len(probability[split]) != len(rows[split]):
            raise ValueError(f"{model_name} returned {len(probability[split])} probabilities for "
                             f"{len(rows[split])} {split} rows")

    cut = threshold.shared_threshold(probability["validation"], rows["validation"]["label"], settings.threshold_rule)
    frame = pd.concat([predictions.make_predictions(rows[split], model_name, probability[split], cut)
                       for split in PREDICTED_SPLITS], ignore_index=True)
    scores = {split: threshold.metrics(probability[split], rows[split]["label"], cut) for split in PREDICTED_SPLITS}

    created = datetime.now(timezone.utc)
    run_id = run_id or f"{created:%Y%m%dT%H%M%SZ}-{model_name}"
    run_dir = Path(runs_dir or settings.runs_dir) / run_id
    if run_dir.exists():
        raise ValueError(f"run folder {run_dir} already exists")
    run_dir.mkdir(parents=True)
    predictions.write_predictions(frame, run_dir / "predictions.parquet", cut)
    _write_json(run_dir / "metrics.json", scores)
    validation = scores["validation"]
    _write_json(run_dir / "validation_counts.json", {
        "data_version": version, "model": model_name, "split": "validation", "threshold": cut,
        "tp": validation["tp"], "fp": validation["fp"], "fn": validation["fn"]})
    _write_json(run_dir / "run.json", {
        "run_id": run_id, "model": model_name, "created_utc": created.isoformat(timespec="seconds"),
        "threshold_rule": settings.threshold_rule, "threshold": cut, "data_version": version,
        "git": git_state(training_data.ROOT), "rows": counts, "inputs": training_data.input_columns(table),
        "settings": _settings_record(settings), "model_details": getattr(model, "details", {})})
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
    details = run["model_details"]
    if details.get("chosen"):
        years = ", ".join(str(fold["year"]) for fold in details["inner_folds"])
        chosen = ", ".join(f"{name} {value:g}" for name, value in details["chosen"].items())
        print(f"chosen on the inner folds {years}: {chosen} ({details['chosen_by']})")
        if details.get("stopped_before_max_rounds") is False:
            print("note: the tree search reached max_rounds without stopping; raise max_rounds in model_params.yaml")
    print(f"threshold {run['threshold']:.6f} ({run['threshold_rule']})")
    print(f"{'split':<11}{'rows':>7}{'surges':>8}{'tp':>6}{'fp':>7}{'fn':>6}{'f1':>9}{'precision':>11}{'recall':>9}{'auc':>9}")
    for split in PREDICTED_SPLITS:
        s = scores[split]
        print(f"{split:<11}{s['rows']:>7}{s['surges']:>8}{s['tp']:>6}{s['fp']:>7}{s['fn']:>6}"
              f"{_fmt(s['f1']):>9}{_fmt(s['precision']):>11}{_fmt(s['recall']):>9}{_fmt(s['auc']):>9}")
    print(f"wrote {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
