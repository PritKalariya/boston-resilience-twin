"""Starting values against tuned ones on validation 2024 (#109, M6): M4's selection (selection.py),
unchanged, run twice on the same data version, splits, threshold rule and seeds: once with the
starting values of model_params.yaml alone, once with tune.py's confirmed candidates over them.

    python Model-Development/scripts/compare_tuned.py [--candidate Model-Development/tuning/best_params.yaml]
        [--id NAME] [--config Model-Development/config/training.csv] [--tuning Model-Development/config/tuning.csv]

It is the comparison #109's PR reports: whether tuned beats default on validation, with the numbers.
It chooses nothing and activates nothing. tuned_params.yaml is neither read nor written, since each
selection points training.csv's tuned_params at its own file: for the starting values one that does
not exist, so model_params.resolve uses the starting values alone, as before M6.

For every model it records both selections' validation F1, precision, recall, log loss, AUC, PR-AUC,
Brier skill, threshold and counts. For a model the candidate file tunes, it adds the share of scored
2024 rows on which both runs make the same surge call, and the paired bootstrap over whole 2024 days
(training.csv's bootstrap_resamples and bootstrap_seed; the same draws for both runs) of the tuned
minus starting F1 and log loss, with their 95% intervals. The test window is not compared: it stays
the selection reports' final check.

Writes <study_dir>/comparison.json and logs one run to tuning.csv's mlflow_experiment, with the two
selections nested in it. The run folders go to training.csv's runs_dir and each selection's reports to
<runs_dir>/<id>-<default or tuned>-reports/, never to reports_dir, the training stage's output.
Exits 0 when both selections ran, and 2 with the reason.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import mlflow_runs
import model_params
import predictions
import selection
import train
import training_data
import tune

METRICS = ("f1", "precision", "recall", "log_loss", "auc", "pr_auc", "brier_skill", "tp", "fp", "fn", "rows", "surges")
GAINS = ("f1", "precision", "recall", "log_loss", "auc", "pr_auc", "brier_skill")
KINDS = ("default", "tuned")


def by_model(content: dict) -> dict[str, dict]:
    """selection.json's record of every model it trained: the ranked candidates and the baseline."""
    records = {record["model"]: record for record in content["ranking"]}
    records[content["baseline"]["model"]] = content["baseline"]
    return records


def validation_rows(settings, record: dict) -> pd.DataFrame:
    """A run's scored validation rows, checked against the prediction contract, in date and unit order."""
    frame = predictions.read_predictions(settings.runs_dir / record["run_folder"] / "predictions.parquet",
                                         record["threshold"])
    return frame[frame["split"] == "validation"].sort_values(["date", "unit"]).reset_index(drop=True)


def _f1(flag: np.ndarray, label: np.ndarray) -> float:
    tp = int(((flag == 1) & (label == 1)).sum())
    fp = int(((flag == 1) & (label == 0)).sum())
    fn = int(((flag == 0) & (label == 1)).sum())
    return 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0


def _log_loss(probability: np.ndarray, label: np.ndarray) -> float:
    p = np.clip(probability, 1e-15, 1 - 1e-15)
    return float(-np.mean(label * np.log(p) + (1 - label) * np.log1p(-p)))


def paired_bootstrap(default: pd.DataFrame, tuned: pd.DataFrame, draws: int, seed: int) -> dict:
    """The tuned minus starting F1 and log loss on the same draws of whole days, with replacement: their
    95% intervals and the share of draws in which tuned came out ahead."""
    if not default[["unit", "date", "label"]].equals(tuned[["unit", "date", "label"]]):
        raise ValueError("the two selections scored different validation rows")
    rng = np.random.default_rng(seed)
    days = default["date"].unique()
    dates = default["date"].to_numpy()
    index = {day: np.flatnonzero(dates == day) for day in days}
    label = default["label"].to_numpy()
    flags = (default["prediction"].to_numpy(), tuned["prediction"].to_numpy())
    probabilities = (default["probability"].to_numpy(), tuned["probability"].to_numpy())
    f1_gain, loss_gain = [], []
    for _ in range(draws):
        rows = np.concatenate([index[day] for day in rng.choice(days, size=len(days), replace=True)])
        f1_gain.append(_f1(flags[1][rows], label[rows]) - _f1(flags[0][rows], label[rows]))
        loss_gain.append(_log_loss(probabilities[1][rows], label[rows]) - _log_loss(probabilities[0][rows], label[rows]))
    f1_gain, loss_gain = np.array(f1_gain), np.array(loss_gain)
    return {"draws": draws, "seed": seed, "days": len(days), "rows": len(default),
            "f1_gain_ci95": [float(np.percentile(f1_gain, 2.5)), float(np.percentile(f1_gain, 97.5))],
            "f1_tuned_higher_share": float(np.mean(f1_gain > 0)),
            "log_loss_gain_ci95": [float(np.percentile(loss_gain, 2.5)), float(np.percentile(loss_gain, 97.5))],
            "log_loss_tuned_lower_share": float(np.mean(loss_gain < 0))}


def _validation(record: dict) -> dict:
    return {**{name: record["validation"].get(name) for name in METRICS}, "threshold": record["threshold"],
            "chosen": record.get("chosen", {}), "run_folder": record["run_folder"]}


def _shown(path: Path) -> str:
    return (path.relative_to(training_data.ROOT) if path.is_relative_to(training_data.ROOT) else path).as_posix()


def compare(settings, tuning, candidate: Path, comparison_id: str | None = None, log=print) -> dict:
    """Run both selections, compare them (see the module docstring), write comparison.json, log the
    comparison to MLflow and return it."""
    tuned_values = model_params.load_tuned(candidate)
    if not tuned_values:
        raise ValueError(f"{candidate} tunes no model, so there is nothing to compare")
    comparison_id = comparison_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-tune-compare"
    absent = settings.runs_dir / f"{comparison_id}-no-tuned-values.yaml"
    if absent.exists():
        raise ValueError(f"{absent} exists, so the starting values would not run alone")
    base = dataclasses.replace(settings, mlflow_experiment=tuning.mlflow_experiment)
    runs = {"default": dataclasses.replace(base, tuned_params=absent),
            "tuned": dataclasses.replace(base, tuned_params=candidate)}
    version = training_data.data_version(settings.dvc_lock)
    git = train.git_state(training_data.ROOT)
    candidate_sha256 = "sha256:" + hashlib.sha256(candidate.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    tags = {"purpose": "tuned against starting values on validation (#109)", "data_version": version,
            "git_commit": git["commit"], "candidate": candidate.name, "candidate_sha256": candidate_sha256}
    contents = {}
    with mlflow_runs.tracked_run(base, comparison_id, tags):
        for kind, kind_settings in runs.items():
            log(f"{kind}: M4's selection with {'the starting values alone' if kind == 'default' else candidate.name}")
            contents[kind], _ = selection.run_selection(kind_settings, f"{comparison_id}-{kind}",
                                                        settings.runs_dir / f"{comparison_id}-{kind}-reports", log)
        versions = {contents[kind]["data_version"] for kind in KINDS}
        if versions != {version}:
            raise ValueError(f"the selections ran on data versions {sorted(versions)}, not {version}")
        records = {kind: by_model(contents[kind]) for kind in KINDS}
        models = {}
        for name, default in records["default"].items():
            tuned = records["tuned"][name]
            entry = {"tuned_values": tuned_values.get(name, {}), "default": _validation(default),
                     "tuned": _validation(tuned),
                     "difference": {metric: tuned["validation"][metric] - default["validation"][metric] for metric in GAINS
                                    if tuned["validation"].get(metric) is not None
                                    and default["validation"].get(metric) is not None}}
            if name in tuned_values:
                rows = {kind: validation_rows(settings, records[kind][name]) for kind in KINDS}
                for kind in KINDS:  # the rows give the F1 the selection reported
                    recount = _f1(rows[kind]["prediction"].to_numpy(), rows[kind]["label"].to_numpy())
                    if abs(recount - records[kind][name]["validation"]["f1"]) > 1e-9:
                        raise ValueError(f"{kind} {name}: its rows give F1 {recount}, not the reported "
                                         f"{records[kind][name]['validation']['f1']}")
                entry["same_call_share"] = float(np.mean(rows["default"]["prediction"].to_numpy()
                                                         == rows["tuned"]["prediction"].to_numpy()))
                entry["bootstrap"] = paired_bootstrap(rows["default"], rows["tuned"], settings.bootstrap_resamples,
                                                      settings.bootstrap_seed)
            models[name] = entry
        result = {"comparison_id": comparison_id, "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "data_version": version, "git": git, "split": "validation", "candidate": _shown(candidate),
                  "candidate_sha256": candidate_sha256, "tuned_values": tuned_values,
                  "selections": {kind: contents[kind]["selection_id"] for kind in KINDS},
                  "winner": {kind: contents[kind]["winner"] for kind in KINDS}, "models": models}
        out = tuning.study_dir / "comparison.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        metrics = {}
        for name, entry in models.items():
            metrics.update({f"{name}_default_f1": entry["default"]["f1"], f"{name}_tuned_f1": entry["tuned"]["f1"]})
            if "bootstrap" in entry:
                boot = entry["bootstrap"]
                metrics.update({f"{name}_f1_gain": entry["difference"]["f1"],
                                f"{name}_f1_gain_ci95_low": boot["f1_gain_ci95"][0],
                                f"{name}_f1_gain_ci95_high": boot["f1_gain_ci95"][1],
                                f"{name}_log_loss_gain": entry["difference"]["log_loss"],
                                f"{name}_log_loss_gain_ci95_low": boot["log_loss_gain_ci95"][0],
                                f"{name}_log_loss_gain_ci95_high": boot["log_loss_gain_ci95"][1],
                                f"{name}_same_call_share": entry["same_call_share"]})
        mlflow_runs.log_summary({"candidate": result["candidate"], "tuned_models": sorted(tuned_values),
                                 "bootstrap_resamples": settings.bootstrap_resamples,
                                 "bootstrap_seed": settings.bootstrap_seed},
                                metrics, {"winner_default": result["winner"]["default"] or "none",
                                          "winner_tuned": result["winner"]["tuned"] or "none"}, [out])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare tuned values with the starting ones on validation 2024 (#109).")
    parser.add_argument("--candidate", help=f"the tuned values file (default: tuning.csv's study_dir/{tune.BEST_PARAMS})")
    parser.add_argument("--id", help="the comparison's name, also the prefix of its run folders (default: UTC time)")
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG), help="training.csv path")
    parser.add_argument("--tuning", default=str(tune.DEFAULT_TUNING), help="tuning.csv path")
    args = parser.parse_args(argv)
    try:
        settings = training_data.load_settings(args.config)
        tuning = tune.load_tuning(args.tuning)
        candidate = Path(args.candidate) if args.candidate else tuning.study_dir / tune.BEST_PARAMS
        if not candidate.is_file():
            raise ValueError(f"{candidate} does not exist: run tune.py first, or name a file with --candidate")
        result = compare(settings, tuning, candidate, args.id)
    except Exception as exc:  # a bad config, a missing file, a failed selection or a check
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"comparison {result['comparison_id']}, data {result['data_version'][:19]}, validation 2024; winner: "
          f"default {result['winner']['default']}, tuned {result['winner']['tuned']}")
    print(f"{'model':<10}{'default F1':>12}{'tuned F1':>10}{'F1 gain':>10}{'95% interval':>20}"
          f"{'log loss gain':>15}{'95% interval':>22}{'same call':>11}")
    for name, entry in result["models"].items():
        line = f"{name:<10}{entry['default']['f1']:>12.6f}{entry['tuned']['f1']:>10.6f}"
        boot = entry.get("bootstrap")
        if boot:
            f1_low, f1_high = boot["f1_gain_ci95"]
            loss_low, loss_high = boot["log_loss_gain_ci95"]
            line += (f"{entry['difference']['f1']:>+10.6f}{f'[{f1_low:+.4f}, {f1_high:+.4f}]':>20}"
                     f"{entry['difference']['log_loss']:>+15.6f}{f'[{loss_low:+.5f}, {loss_high:+.5f}]':>22}"
                     f"{entry['same_call_share']:>11.1%}")
        else:
            line += f"{'(not tuned)':>12}"
        print(line)
    print(f"wrote {_shown(tuning.study_dir / 'comparison.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
