"""Hyperparameter tuning with Optuna (#109, M6): settings for the three candidates, searched and then
confirmed on the inner folds of the train years, so validation 2024 stays out of every choice.

    python Model-Development/scripts/tune.py [--models logreg;lightgbm] [--trials N] [--activate]
        [--config Model-Development/config/training.csv] [--tuning Model-Development/config/tuning.csv]

For each model in tuning.csv it runs one Optuna study, with the TPE sampler, a fixed seed and a fixed
number of trials, and then confirms the study's best trials on seeds no trial used:

1. Trial 0 is the starting values of model_params.yaml, so the study compares tuning with them. For
   logistic regression that is the C its grid picks on the inner folds, with the starting curve.
2. A trial builds the candidate with its settings (SPACES, every other parameter at its starting
   value) and fits it on the train rows (models.py, M4) once per trial seed, or once for a candidate
   with no seed. Each fit's own inner-fold search picks its trees or its C (inner_folds.py); each
   fold year is then refit on the train days before it with that choice and predicted. A fit's fold
   F1 is the mean over the fold years of the F1 at the threshold the shared rule (threshold.py) picks
   on that year itself, as the gates score validation 2024. Optuna maximizes the trial's mean fold F1
   over its seeds, since the seed alone moves one fit's fold F1 by more than most gains between trials.
3. Confirmation: trial 0 and the confirm_top best other trials are fitted again with confirm_seeds,
   seeds the study never used. A setting is eligible when its mean inner-fold log loss (the score M4's
   early stopping uses) is at most guard_ratio times trial 0's. The eligible setting with the highest
   mean fold F1 is the candidate (then the higher time-safe F1: fold 2022 at the threshold picked on
   2019, 2023 at the one picked on both; then the lower log loss). It is an improvement only when its
   mean gain over trial 0, paired by seed, is more than stable_se standard errors of the paired gains;
   otherwise the starting values are kept. Logistic regression has no seed, so its pairs are the fold
   years and it refits what the study already scored: a weaker check, with three pairs, no fresh fits
   and the study's luck in ranking its best trials left in. The confirmation records which pairs it used.

Validation 2024 and test are never read: the table is cut to the train split before any trial, so
the threshold and the winner stay 2024's alone (inner_folds.py). The number of trees is not tuned,
since the inner-fold search always picks it (model_params.py).

Writes:
- <study_dir>/best_params.yaml: each improvement's values that differ from the starting ones, in the
  model_params.yaml format and checked by model_params.load_tuned; a candidate file that no training
  run reads.
- tuned_params.yaml (training.csv's tuned_params), the file models.build reads, only with --activate,
  which copies best_params.yaml's values into it. Without --activate it is left as it is, so a tuning
  run never changes the model M4 trains: a promotion is a reviewed step (docs/runbook.md).
- <study_dir>/<model>.json: the study with every trial's settings and scores and the confirmation,
  enough to rebuild the study (load_study) for M10 (#113); <study_dir>/importance.csv: each setting's
  importance for the log loss and for the fold F1 (Optuna's fANOVA, seeded); <study_dir>/summary.json:
  what each confirmation found.
- MLflow (tuning.csv's mlflow_experiment): one run per study, with a child run per trial and per
  confirmed setting, and a tune-summary run with the files above.

Every setting comes from the two config files; --models and --trials only narrow a run, which then
writes the files of its own models alone, and the summary records them. Exits 0 when every study ran
and the files are written, and 2 with the reason.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import optuna
import yaml

import inner_folds
import mlflow_runs
import model_params
import models
import threshold
import train
import training_data

DEFAULT_TUNING = training_data.ROOT / "Model-Development" / "config" / "tuning.csv"
TUNING_SETTINGS = ("models", "trials", "seed", "trial_seeds", "confirm_seeds", "confirm_top", "guard_ratio",
                   "stable_se", "study_dir", "mlflow_experiment")
MODEL_CLASSES = {"logreg": models.LogisticModel, "xgboost": models.XGBoostModel, "lightgbm": models.LightGBMModel}
BEST_PARAMS = "best_params.yaml"  # in study_dir: the confirmed candidates, which no training run reads

Float, Int = optuna.distributions.FloatDistribution, optuna.distributions.IntDistribution

# The search space of each candidate: what a trial may change, and over what range. Every other
# parameter keeps its starting value in model_params.yaml, the number of trees included (the
# inner-fold search picks it). Each range holds the starting value, so trial 0 is a point of it. The
# tree models' ranges are the #109 review's; the order of the settings is part of the study, since the
# sampler draws them in this order.
SPACES = {
    "logreg": {
        # The inverse of the regularization strength, over the range of M4's C_grid (0.001 to 100),
        # on a log scale. A trial's C is written as a C_grid of one value, which fixes C.
        "C": Float(0.001, 100.0, log=True),
        # day_of_year's periodic curve in this many parts: 3 (about four months each, the fewest the
        # periodic curve takes, model_params.py) to 24 (about two weeks). The first 100-trial study put
        # 9 of its 10 lowest log losses at 4, the old minimum, so the range now reaches 3.
        "year_parts": Int(3, 24),
    },
    "xgboost": {
        # The step each new tree takes. A smaller one needs more trees, which early stopping finds up
        # to max_rounds (3000); a LightGBM fit at 0.01 stopped at 368 trees (#109 review).
        "learning_rate": Float(0.01, 0.15, log=True),
        # Levels per tree: deeper trees can fit rarer patterns, and noise.
        "max_depth": Int(2, 8),
        # The least total p(1 - p) a leaf may hold (5 is about 150 rows at the surge rate).
        "min_child_weight": Float(0.5, 50.0, log=True),
        # The L2 brake on leaf values.
        "reg_lambda": Float(0.01, 20.0, log=True),
        # The share of rows and of inputs each tree sees.
        "subsample": Float(0.65, 1.0),
        "colsample_bytree": Float(0.6, 1.0),
    },
    "lightgbm": {
        # As for XGBoost.
        "learning_rate": Float(0.01, 0.15, log=True),
        # Leaves per tree, LightGBM's bound on a tree's size (it grows trees leaf by leaf), on a log
        # scale from 4: Run 2's lowest log losses had 5 and 6 leaves.
        "num_leaves": Int(4, 63, log=True),
        # The least rows a leaf may hold, on a log scale.
        "min_child_samples": Int(20, 200, log=True),
        "reg_lambda": Float(0.01, 20.0, log=True),
        # The share of rows (redrawn every subsample_freq trees) and of inputs each tree sees.
        "subsample": Float(0.65, 1.0),
        "colsample_bytree": Float(0.6, 1.0),
    },
}


@dataclass(frozen=True)
class Tuning:
    models: tuple[str, ...]
    trials: int
    seed: int  # the TPE sampler's and the fANOVA importance's
    trial_seeds: tuple[int, ...]  # the model seeds every trial is fitted with
    confirm_seeds: tuple[int, ...]  # the fresh model seeds of the confirmation
    confirm_top: int
    guard_ratio: float
    stable_se: float
    study_dir: Path
    mlflow_experiment: str


def _seed_list(name: str, value: str, least: int) -> tuple[int, ...]:
    seeds = tuple(training_data._whole(name, part, 0) for part in value.split(";") if part.strip())
    if len(seeds) < least or len(set(seeds)) != len(seeds):
        raise training_data.SettingsError(
            f"{name} needs at least {least} different whole numbers separated by ;, got {value!r}")
    return seeds


def _number(name: str, value: str, low: float, low_allowed: bool) -> float:
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not (math.isfinite(number) and (number >= low if low_allowed else number > low)):
        bound = f"of {low:g} or more" if low_allowed else f"above {low:g}"
        raise training_data.SettingsError(f"{name} must be a number {bound}, got {value!r}")
    return number


def load_tuning(path: str | Path = DEFAULT_TUNING, root: str | Path = training_data.ROOT) -> Tuning:
    """Load and check tuning.csv. Fails, naming the setting, on a missing, unknown or duplicate
    setting, or a bad value."""
    path, root = Path(path), Path(root)
    pairs = training_data._read_pairs(path)
    unknown = sorted(set(pairs) - set(TUNING_SETTINGS))
    if unknown:
        raise training_data.SettingsError(f"unknown setting(s) in {path.name}: {unknown}")
    missing = [name for name in TUNING_SETTINGS if name not in pairs]
    if missing:
        raise training_data.SettingsError(f"missing setting(s) in {path.name}: {missing}")
    names = tuple(name.strip() for name in pairs["models"].split(";") if name.strip())
    if not names:
        raise training_data.SettingsError("models needs at least one model")
    strangers = [name for name in names if name not in SPACES]
    if strangers or len(set(names)) != len(names):
        raise training_data.SettingsError(f"models must name each of {sorted(SPACES)} at most once, got {list(names)}")
    trials = training_data._whole("trials", pairs["trials"], 1)
    seed = training_data._whole("seed", pairs["seed"], 0)
    trial_seeds = _seed_list("trial_seeds", pairs["trial_seeds"], 1)
    confirm_seeds = _seed_list("confirm_seeds", pairs["confirm_seeds"], 2)
    reused = sorted(set(trial_seeds) & set(confirm_seeds))
    if reused:
        raise training_data.SettingsError(f"confirm_seeds must be fresh, but {reused} are also trial_seeds")
    confirm_top = training_data._whole("confirm_top", pairs["confirm_top"], 1)
    guard_ratio = _number("guard_ratio", pairs["guard_ratio"], 1.0, True)
    stable_se = _number("stable_se", pairs["stable_se"], 0.0, False)
    if not pairs["study_dir"]:
        raise training_data.SettingsError("study_dir needs a path")
    study_dir = Path(pairs["study_dir"])
    if not pairs["mlflow_experiment"]:
        raise training_data.SettingsError("mlflow_experiment needs a name")
    return Tuning(models=names, trials=trials, seed=seed, trial_seeds=trial_seeds, confirm_seeds=confirm_seeds,
                  confirm_top=confirm_top, guard_ratio=guard_ratio, stable_se=stable_se,
                  study_dir=study_dir if study_dir.is_absolute() else root / study_dir,
                  mlflow_experiment=pairs["mlflow_experiment"])


def model_values(name: str, params: dict) -> dict:
    """The model_params.yaml entries a trial's settings give: logistic regression's C becomes a
    C_grid of one value; every other setting keeps its name."""
    return {("C_grid" if name == "logreg" and key == "C" else key): ([value] if name == "logreg" and key == "C" else value)
            for key, value in params.items()}


def _suggest(trial: optuna.Trial, key: str, distribution) -> float | int:
    if isinstance(distribution, Int):
        return trial.suggest_int(key, distribution.low, distribution.high, log=distribution.log)
    return trial.suggest_float(key, distribution.low, distribution.high, log=distribution.log)


def _shares(counts) -> dict:
    """F1, precision and recall of a threshold's counts; 0 where a share has no denominator."""
    def share(top: int, bottom: int) -> float:
        return top / bottom if bottom else 0.0
    return {"f1": share(2 * counts.tp, 2 * counts.tp + counts.fp + counts.fn),
            "precision": share(counts.tp, counts.tp + counts.fp), "recall": share(counts.tp, counts.tp + counts.fn)}


@dataclass(frozen=True)
class Fit:
    """One fit's scores on the inner folds (see the module docstring, point 2)."""
    log_loss: float  # the mean inner-fold log loss the fit's own search reports
    log_loss_by_fold: dict
    folds: dict  # fold year -> f1, precision and recall at the threshold picked on that year, with it and pr_auc
    forward: dict  # each fold year after the first -> its F1 at the threshold picked on the years before it, pooled
    chosen: dict  # what the fit picked on the folds: its trees, or its C
    seconds: float

    @property
    def f1(self) -> float:
        """The fold F1: the mean over the fold years of the F1 at each year's own threshold."""
        return float(np.mean([fold["f1"] for fold in self.folds.values()]))

    @property
    def forward_f1(self) -> float:
        """The time-safe F1: the mean F1 of the later fold years at the earlier ones' thresholds; 0 with one fold."""
        return float(np.mean(list(self.forward.values()))) if self.forward else 0.0

    @property
    def pr_auc(self) -> float:
        return float(np.mean([fold["pr_auc"] for fold in self.folds.values()]))

    def record(self, seed: int | None) -> dict:
        return {"seed": seed, "f1": self.f1, "forward_f1": self.forward_f1, "pr_auc": self.pr_auc,
                "log_loss": self.log_loss, "log_loss_by_fold": self.log_loss_by_fold, "folds": self.folds,
                "forward": self.forward, "chosen": self.chosen, "seconds": self.seconds}


def _fold_estimator(name: str, model):
    """A fresh estimator with the settings the fitted model chose, to refit on one fold."""
    if name == "logreg":
        pipeline = model._pipeline()
        pipeline.set_params(model__C=model.details["chosen"]["C"])
        return pipeline
    return model._final_model(model.details["chosen"]["trees"])


def evaluate(name: str, values: dict, rows, inputs: list[str], fold_years: tuple[int, ...], rule: str) -> Fit:
    """Fit candidate name with values (every model_params.yaml parameter of it) on rows, the train
    split, and score it on the inner folds (see the module docstring, point 2)."""
    started = time.perf_counter()
    label, weight = rows["label"].to_numpy(), rows["weight"].to_numpy(dtype="float64")
    model = MODEL_CLASSES[name](inputs, fold_years, **values)
    model.fit(rows, label, weight)
    data = rows[inputs]
    predicted = {}
    for fold in inner_folds.make_folds(rows["date"], label, fold_years):
        estimator = _fold_estimator(name, model)
        weights = {("model__sample_weight" if name == "logreg" else "sample_weight"): weight[fold.fit]}
        estimator.fit(data.iloc[fold.fit], label[fold.fit], **weights)
        predicted[str(fold.year)] = (estimator.predict_proba(data.iloc[fold.check])[:, 1], label[fold.check])
    years, folds, forward = list(predicted), {}, {}
    for k, year in enumerate(years):
        p, y = predicted[year]
        cut = threshold.shared_threshold(p, y, rule)
        folds[year] = {**_shares(threshold.counts(p, y, cut)), "threshold": float(cut),
                       "pr_auc": float(threshold.pr_auc(p, y))}
        if k:  # time-safe: the threshold the earlier fold years pick, pooled
            before = [predicted[earlier] for earlier in years[:k]]
            cut = threshold.shared_threshold(np.concatenate([b[0] for b in before]),
                                             np.concatenate([b[1] for b in before]), rule)
            forward[year] = _shares(threshold.counts(p, y, cut))["f1"]
    return Fit(log_loss=float(model.details["inner_log_loss"]),
               log_loss_by_fold=dict(model.details["inner_log_loss_by_fold"]), folds=folds, forward=forward,
               chosen=dict(model.details["chosen"]), seconds=round(time.perf_counter() - started, 3))


def fit_seeds(name: str, tuning: Tuning, confirm: bool = False) -> tuple[int | None, ...]:
    """The seeds candidate name is fitted with: trial_seeds, or confirm_seeds in the confirmation, for
    a candidate with a seed parameter; a single fit (None) for one without, logistic regression."""
    if "seed" not in model_params.PARAMETERS[name]:
        return (None,)
    return tuning.confirm_seeds if confirm else tuning.trial_seeds


def score(name: str, values: dict, seeds: tuple[int | None, ...], rows, inputs: list[str], settings) -> list[Fit]:
    """One fit of candidate name with values per seed; a seed replaces the starting one."""
    return [evaluate(name, values if seed is None else {**values, "seed": seed}, rows, inputs,
                     settings.inner_fold_years, settings.threshold_rule) for seed in seeds]


def means(fits: list[Fit]) -> dict:
    """The scores of fits, one per seed, averaged; "f1" is a trial's value."""
    years = list(fits[0].folds)

    def mean(values) -> float:
        return float(np.mean(list(values)))
    return {"f1": mean(f.f1 for f in fits), "log_loss": mean(f.log_loss for f in fits),
            "forward_f1": mean(f.forward_f1 for f in fits), "pr_auc": mean(f.pr_auc for f in fits),
            "precision": mean(mean(f.folds[y]["precision"] for y in years) for f in fits),
            "recall": mean(mean(f.folds[y]["recall"] for y in years) for f in fits),
            "f1_sd": float(np.std([f.f1 for f in fits], ddof=1)) if len(fits) > 1 else 0.0,
            "fold_f1": {y: mean(f.folds[y]["f1"] for f in fits) for y in years},
            "fold_log_loss": {y: mean(f.log_loss_by_fold[y] for f in fits) for y in years},
            "seconds": round(sum(f.seconds for f in fits), 3)}


def _metrics(scores: dict) -> dict[str, float]:
    """A trial's or a confirmed setting's scores as MLflow metrics."""
    flat = {"fold_f1": scores["f1"], "inner_log_loss": scores["log_loss"], "forward_f1": scores["forward_f1"],
            "pr_auc": scores["pr_auc"], "precision": scores["precision"], "recall": scores["recall"],
            "fold_f1_sd": scores["f1_sd"], "seconds": scores["seconds"]}
    flat.update({f"fold_f1_{year}": value for year, value in scores["fold_f1"].items()})
    flat.update({f"inner_log_loss_{year}": value for year, value in scores["fold_log_loss"].items()})
    return flat


def importances(study: optuna.Study, seed: int) -> dict[str, dict[str, float | None]]:
    """Each setting's fANOVA importance (seeded) for the log loss and for the fold F1, the study's
    value, as {setting: {"log_loss": share, "f1": share}}. A score that took one value in every trial
    has no importance to share out, so its shares are None; with fewer than 2 trials there is nothing."""
    done = study.get_trials(states=(optuna.trial.TrialState.COMPLETE,))
    if len(done) < 2:
        return {}
    shares: dict[str, dict[str, float | None]] = {key: {} for key in done[0].params}
    for name, target in (("log_loss", lambda t: t.user_attrs["scores"]["log_loss"]), ("f1", lambda t: t.value)):
        if len({target(t) for t in done}) < 2:
            found = {}
        else:
            found = optuna.importance.get_param_importances(
                study, evaluator=optuna.importance.FanovaImportanceEvaluator(seed=seed), target=target)
        for key in shares:
            shares[key][name] = float(found[key]) if key in found else None
    return shares


def _trial_record(trial: optuna.trial.FrozenTrial) -> dict:
    return {"number": trial.number, "state": trial.state.name, "params": trial.params, "value": trial.value,
            "user_attrs": trial.user_attrs}


def load_study(path: str | Path) -> optuna.Study:
    """An in-memory Optuna study rebuilt from a <model>.json that tune.py wrote, with every completed
    trial, its settings, fold F1 and user attributes (its mean scores and its fits): for M10's
    importance and slice plots (#113)."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    distributions = {key: optuna.distributions.json_to_distribution(value)
                     for key, value in record["distributions"].items()}
    study = optuna.create_study(direction=record["direction"], study_name=record["study_name"])
    study.add_trials([optuna.trial.create_trial(params=t["params"], distributions=distributions, value=t["value"],
                                                user_attrs=t["user_attrs"])
                      for t in record["trials"] if t["state"] == "COMPLETE"])
    return study


def run_study(name: str, rows, inputs: list[str], settings, tuning: Tuning, starting: dict, trials: int,
              log=print) -> optuna.Study:
    """One study for candidate name (see the module docstring, points 1 and 2), each trial logged as a
    child of the active MLflow run."""
    fold_years, rule = settings.inner_fold_years, settings.threshold_rule
    space, seeds = SPACES[name], fit_seeds(name, tuning)
    start = {key: starting[key] for key in space if key in starting}
    if name == "logreg":
        # trial 0: the C the starting grid picks on the folds, with the starting curve
        start = {"C": evaluate(name, starting, rows, inputs, fold_years, rule).chosen["C"],
                 "year_parts": starting["year_parts"]}
    tuning_settings = dataclasses.replace(settings, mlflow_experiment=tuning.mlflow_experiment)
    study = optuna.create_study(direction="maximize", study_name=f"tune-{name}",
                                sampler=optuna.samplers.TPESampler(seed=tuning.seed))
    study.enqueue_trial(start)

    def objective(trial: optuna.Trial) -> float:
        params = {key: _suggest(trial, key, distribution) for key, distribution in space.items()}
        fits = score(name, {**starting, **model_values(name, params)}, seeds, rows, inputs, settings)
        scores = means(fits)
        trial.set_user_attr("scores", scores)
        trial.set_user_attr("fits", [fit.record(seed) for seed, fit in zip(seeds, fits)])
        with mlflow_runs.tracked_run(tuning_settings, f"tune-{name}-trial-{trial.number}",
                                     {"model": name, "trial": trial.number}):
            mlflow_runs.log_summary({**params, "chosen": [fit.chosen for fit in fits]}, _metrics(scores), {}, [])
        log(f"  {name} trial {trial.number:>3}: fold F1 {scores['f1']:.4f}, log loss {scores['log_loss']:.6f}, "
            f"{[fit.chosen for fit in fits]}, {scores['seconds']:.1f} s")
        return scores["f1"]

    study.optimize(objective, n_trials=trials)
    return study


def confirm(name: str, study: optuna.Study, rows, inputs: list[str], settings, tuning: Tuning, starting: dict,
            log=print) -> dict:
    """Fit trial 0 and the confirm_top best other trials again and apply the rule (see the module
    docstring, point 3), each setting logged as a child of the active MLflow run. Returns the
    confirmation: the pool with its scores, the candidate trial (0 when the starting values rank
    first), whether it is an improvement, and the decision."""
    first = study.trials[0]
    pool, seen = [first], {json.dumps(first.params, sort_keys=True)}
    for trial in sorted(study.get_trials(states=(optuna.trial.TrialState.COMPLETE,)), key=lambda t: (-t.value, t.number)):
        if len(pool) > tuning.confirm_top:
            break
        key = json.dumps(trial.params, sort_keys=True)
        if trial.number != 0 and key not in seen:
            pool.append(trial)
            seen.add(key)
    seeds = fit_seeds(name, tuning, confirm=True)
    by_seed = seeds != (None,)
    tuning_settings = dataclasses.replace(settings, mlflow_experiment=tuning.mlflow_experiment)
    entries = []
    for trial in pool:
        fits = score(name, {**starting, **model_values(name, trial.params)}, seeds, rows, inputs, settings)
        entries.append({"trial": trial.number, "params": trial.params, "fits": fits, "scores": means(fits)})
    base = entries[0]
    guard = tuning.guard_ratio * base["scores"]["log_loss"]
    for entry in entries:
        if by_seed:
            gains = [fit.f1 - first_fit.f1 for fit, first_fit in zip(entry["fits"], base["fits"])]
        else:  # one fit each: the fold years are the pairs
            gains = [entry["fits"][0].folds[y]["f1"] - base["fits"][0].folds[y]["f1"] for y in base["fits"][0].folds]
        entry["gain"] = float(np.mean(gains))
        entry["gain_se"] = float(np.std(gains, ddof=1) / math.sqrt(len(gains))) if len(gains) > 1 else 0.0
        entry["eligible"] = entry["scores"]["log_loss"] <= guard
        # one pair has no standard error, so it never shows an improvement
        entry["improvement"] = (entry["trial"] != 0 and len(gains) > 1
                                and entry["gain"] > tuning.stable_se * entry["gain_se"])
        with mlflow_runs.tracked_run(tuning_settings, f"tune-{name}-confirm-trial-{entry['trial']}",
                                     {"model": name, "trial": entry["trial"], "confirmation": True}):
            mlflow_runs.log_summary(entry["params"], {**_metrics(entry["scores"]), "gain": entry["gain"],
                                                      "gain_se": entry["gain_se"], "eligible": float(entry["eligible"]),
                                                      "improvement": float(entry["improvement"])}, {}, [])
        log(f"  {name} confirm trial {entry['trial']:>3}: fold F1 {entry['scores']['f1']:.4f}, gain {entry['gain']:+.4f} "
            f"(SE {entry['gain_se']:.4f}), log loss {entry['scores']['log_loss']:.6f}"
            f"{'' if entry['eligible'] else ', over the log loss guard'}")
    ranked = sorted((e for e in entries if e["eligible"]),
                    key=lambda e: (-e["scores"]["f1"], -e["scores"]["forward_f1"], e["scores"]["log_loss"]))
    candidate = ranked[0]  # trial 0 is always eligible: guard_ratio is 1 or more
    if candidate["improvement"]:
        decision = "improvement: tuned values in best_params.yaml"
    elif candidate["trial"] == 0:
        decision = "starting values kept: trial 0 ranked first"
    else:
        decision = "starting values kept: the candidate's gain is within stable_se standard errors"
    return {"pairs": "confirm_seeds" if by_seed else "fold years (no seed: one fit per setting)",
            "seeds": list(seeds) if by_seed else [], "guard_log_loss": guard, "stable_se": tuning.stable_se,
            "candidate": candidate["trial"], "improvement": candidate["improvement"], "decision": decision,
            "pool": [{**{key: e[key] for key in ("trial", "params", "scores", "gain", "gain_se", "eligible", "improvement")},
                      "fits": [fit.record(seed) for seed, fit in zip(seeds, e["fits"])]} for e in entries]}


def tuned_entry(name: str, params: dict, starting: dict) -> dict:
    """The model_params.yaml values a trial's settings give that differ from the starting ones."""
    values = model_values(name, params)
    return {key: value for key, value in values.items()
            if (tuple(value) if isinstance(value, list) else value) != starting[key]}


def write_tuned(path: Path, entries: dict, header: list[str]) -> None:
    """Write a tuned values file (only the models and values that changed) and check it as models.build
    would read it. An entry-free file sets nothing (model_params.py)."""
    # Block style, as model_params.yaml reads; values exactly as the trials scored them
    body = yaml.safe_dump(entries, sort_keys=True, default_flow_style=False) if entries else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"# {line}\n" for line in header) + body, encoding="utf-8")
    model_params.load_tuned(path)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tune(settings, tuning: Tuning, names: tuple[str, ...] | None = None, trials: int | None = None,
         activate: bool = False, log=print) -> dict:
    """Run and confirm a study per model, write best_params.yaml and the study files (and, with
    activate, tuned_params.yaml) and return the summary."""
    names, trials = names or tuning.models, trials or tuning.trials
    table, _ = training_data.load_table(settings)
    inputs = training_data.input_columns(table)
    training_data.check_missing_gaps(table, inputs, settings.missing_gap_max_points)
    rows = table[table["split"] == "train"].reset_index(drop=True)  # 2024 and test never reach a trial
    if rows.empty:
        raise ValueError("the train split has no labeled rows")
    starting_values = model_params.load(settings.model_params)
    version = training_data.data_version(settings.dvc_lock)
    git = train.git_state(training_data.ROOT)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    tuning.study_dir.mkdir(parents=True, exist_ok=True)
    tuning_settings = dataclasses.replace(settings, mlflow_experiment=tuning.mlflow_experiment)
    method = {"seed": tuning.seed, "trials": trials, "trial_seeds": list(tuning.trial_seeds),
              "confirm_seeds": list(tuning.confirm_seeds), "confirm_top": tuning.confirm_top,
              "guard_ratio": tuning.guard_ratio, "stable_se": tuning.stable_se}

    entries, results, importance_rows = {}, {}, []
    for name in names:
        starting = starting_values[name]
        seeds = fit_seeds(name, tuning)
        log(f"{name}: {trials} trials on {len(rows):,} train rows, inner folds {list(settings.inner_fold_years)}, "
            f"{'seeds ' + str(list(seeds)) if seeds != (None,) else 'one fit per trial (no seed)'}")
        tags = {"model": name, "data_version": version, "git_commit": git["commit"], **method}
        with mlflow_runs.tracked_run(tuning_settings, f"tune-{name}", tags):
            study = run_study(name, rows, inputs, settings, tuning, starting, trials, log)
            confirmation = confirm(name, study, rows, inputs, settings, tuning, starting, log)
            base = confirmation["pool"][0]
            candidate = next(e for e in confirmation["pool"] if e["trial"] == confirmation["candidate"])
            entry = tuned_entry(name, candidate["params"], starting) if confirmation["improvement"] else {}
            if entry:
                entries[name] = entry
            weights = importances(study, tuning.seed)
            first = study.trials[0]
            best = max(study.get_trials(states=(optuna.trial.TrialState.COMPLETE,)), key=lambda t: (t.value, -t.number))
            results[name] = {
                "trials": len(study.trials), "best_trial": best.number,
                "trial_0": {"fold_f1": first.value, "log_loss": first.user_attrs["scores"]["log_loss"], "params": first.params},
                "best": {"fold_f1": best.value, "log_loss": best.user_attrs["scores"]["log_loss"], "params": best.params},
                "confirmation": {"pairs": confirmation["pairs"], "seeds": confirmation["seeds"],
                                 "candidate_trial": candidate["trial"], "candidate_params": candidate["params"],
                                 "trial_0_fold_f1": base["scores"]["f1"], "candidate_fold_f1": candidate["scores"]["f1"],
                                 "trial_0_log_loss": base["scores"]["log_loss"],
                                 "candidate_log_loss": candidate["scores"]["log_loss"], "gain": candidate["gain"],
                                 "gain_se": candidate["gain_se"], "improvement": confirmation["improvement"],
                                 "decision": confirmation["decision"]},
                "tuned_values": entry,
            }
            study_file = tuning.study_dir / f"{name}.json"
            _write_json(study_file, {
                "model": name, "study_name": study.study_name, "direction": "maximize",
                "objective": "mean fold F1 over the trial seeds: the mean over the inner fold years of the F1 at "
                "the threshold the shared rule picks on each year", "data_version": version, "git": git, **method,
                "inner_fold_years": list(settings.inner_fold_years), "train_rows": len(rows),
                "optuna": optuna.__version__, "best_trial": best.number,
                "distributions": {key: optuna.distributions.distribution_to_json(d) for key, d in SPACES[name].items()},
                "trials": [_trial_record(t) for t in study.trials], "confirmation": confirmation})
            for key, share in weights.items():  # a blank share: that score never changed across the trials
                importance_rows.append({"model": name, "setting": key, **{
                    f"importance_{score_name}": "" if share[score_name] is None else round(share[score_name], 6)
                    for score_name in ("log_loss", "f1")}})
            mlflow_runs.log_summary(
                {"candidate_trial": candidate["trial"], **{f"candidate_{k}": v for k, v in candidate["params"].items()}},
                {"trial_0_fold_f1": base["scores"]["f1"], "candidate_fold_f1": candidate["scores"]["f1"],
                 "trial_0_inner_log_loss": base["scores"]["log_loss"],
                 "candidate_inner_log_loss": candidate["scores"]["log_loss"], "candidate_gain": candidate["gain"],
                 "candidate_gain_se": candidate["gain_se"], "study_best_fold_f1": best.value,
                 "improvement": float(confirmation["improvement"]),
                 **{f"importance_{score_name}_{key}": share[score_name] for key, share in weights.items()
                    for score_name in ("log_loss", "f1") if share[score_name] is not None}},
                {"decision": confirmation["decision"]}, [study_file])
        log(f"{name}: trial 0 fold F1 {first.value:.4f}, best trial {best.number} {best.value:.4f}; confirmed candidate "
            f"trial {candidate['trial']}, gain {candidate['gain']:+.4f} (SE {candidate['gain_se']:.4f}): "
            f"{confirmation['decision']}")

    importance_file = tuning.study_dir / "importance.csv"
    with open(importance_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["model", "setting", "importance_log_loss", "importance_f1"])
        writer.writeheader()
        writer.writerows(importance_rows)
    shown = (tuning.study_dir.relative_to(training_data.ROOT) if tuning.study_dir.is_relative_to(training_data.ROOT)
             else tuning.study_dir).as_posix()
    best_file = tuning.study_dir / BEST_PARAMS
    write_tuned(best_file, entries, [
        f"Confirmed tuning candidates of Model-Development/scripts/tune.py (#109), data version {version}",
        f"seed {tuning.seed}, {trials} trials per model, trial seeds {';'.join(map(str, tuning.trial_seeds))}, "
        f"confirmed on seeds {';'.join(map(str, tuning.confirm_seeds))}; models {', '.join(names)}; studies in {shown}",
        "Not active: no training run reads this file. tune.py --activate copies it into tuned_params.yaml.",
        "A model not named kept its starting values in model_params.yaml."])
    if activate:
        write_tuned(settings.tuned_params, entries, [
            f"Activated by tune.py --activate (#109) from {shown}/{BEST_PARAMS}, data version {version}",
            "Each value replaces its starting one in model_params.yaml; a model not named keeps its starting values."])
    summary = {"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data_version": version,
               "git": git, **method, "models": list(names), "train_rows": len(rows),
               "inner_fold_years": list(settings.inner_fold_years), "results": results, "best_params": BEST_PARAMS,
               "activated": activate, "tuned_params": settings.tuned_params.name}
    summary_file = tuning.study_dir / "summary.json"
    _write_json(summary_file, summary)
    with mlflow_runs.tracked_run(tuning_settings, "tune-summary", {"data_version": version, "git_commit": git["commit"],
                                                                   "models": ";".join(names), "activated": activate}):
        mlflow_runs.log_summary({"models": list(names), **method},
                                {f"{name}_improvement": float(result["confirmation"]["improvement"])
                                 for name, result in results.items()}, {}, [summary_file, importance_file, best_file])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tune the candidates with Optuna on the inner folds (#109).")
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG), help="training.csv path")
    parser.add_argument("--tuning", default=str(DEFAULT_TUNING), help="tuning.csv path")
    parser.add_argument("--models", help="tune only these models, separated by ; (default: tuning.csv's models)")
    parser.add_argument("--trials", type=int, help="trials per study instead of tuning.csv's, for a timing run")
    parser.add_argument("--activate", action="store_true",
                        help=f"also copy {BEST_PARAMS} into tuned_params.yaml, the file M4's training reads "
                             "(off by default, so a tuning run never changes the trained model)")
    args = parser.parse_args(argv)
    try:
        settings = training_data.load_settings(args.config)
        tuning = load_tuning(args.tuning)
        names = None
        if args.models:
            names = tuple(name.strip() for name in args.models.split(";") if name.strip())
            strangers = [name for name in names if name not in tuning.models]
            if strangers:
                raise ValueError(f"--models names {strangers}, not in tuning.csv's models {list(tuning.models)}")
        if args.trials is not None and args.trials < 1:
            raise ValueError("--trials must be 1 or more")
        summary = tune(settings, tuning, names, args.trials, args.activate)
    except Exception as exc:  # a bad config, missing data or a failed check: report it and stop
        print(f"error: {exc}", file=sys.stderr)
        return 2
    decisions = "; ".join(f"{m}: {r['confirmation']['decision']}" for m, r in summary["results"].items())
    print(f"wrote {tuning.study_dir.as_posix()}/ ({decisions}); "
          + (f"activated {settings.tuned_params.as_posix()}" if args.activate
             else f"{settings.tuned_params.name} not changed (--activate copies {BEST_PARAMS} into it)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
