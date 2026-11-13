"""Sensitivity analysis of the selected model (#113, M10): which inputs move its forecasts on the test
split (SHAP values), and how much each tuning setting mattered in M6's studies (#109).

    python Model-Development/scripts/sensitivity.py [--config Model-Development/config/training.csv]
        [--tuning Model-Development/config/tuning.csv] [--selection FILE] [--out DIR]

SHAP, for the model selection.json names (the winner; with none, the best candidate, marked as such):
- It loads that run's model.joblib (#107) and scores the scored test rows of the same data version
  (ADR-003: 2025-01-01 to 2026-06-30). It stops when selection.json comes from another data version
  or when the model does not give back its run's own test probabilities (predictions.parquet), so the
  explanations are of that model on those rows.
- LightGBM and XGBoost: shap's TreeExplainer, exact, on every row. Any other model: shap's permutation
  explainer on a seeded sample of SAMPLE_ROWS rows against a seeded background of train rows. Both are
  in log-odds of the surge probability: a row's values plus the base value give back its probability,
  which is checked (a tree model stops on a gap above ADD_UP_TOLERANCE).
- shap_importance.csv: each input's mean absolute SHAP value, its share of the total, its direction
  (the rank correlation of the input's value with its SHAP value) and its missing share; shap_bar.png
  and shap_beeswarm.png show them. Three example rows, in shap_examples.csv and
  shap_example_<kind>.png: the surest true alarm, the surest false alarm, and the missed surge with
  the lowest probability.

Hyperparameters, from each <model>.json that M6's tune.py wrote (tuning.csv's study_dir), rebuilt with
tune.load_study: each setting's seeded fANOVA importance for the fold F1 (the study's objective) and
for the inner log loss, computed as tune.importances computes importance.csv (hp_importance.csv and
hp_importance_<model>.png), and compared with it: the same on the machine that ran the study, and
within 0.004 elsewhere in the same order of settings, since fANOVA's random forest rounds
differently on macOS and Linux (measured 2026-11-12); and a slice plot of every trial's fold
F1 against the two settings with the highest F1 importance (hp_slice_<model>.png). A model without a
study file is noted, not fatal.

SHAP values describe the model, not the world: an input with a large share is one the model leans
on, an association in the test data, never a cause of 311 surges, and inputs that move together
share credit in ways the real world need not (report.md says so beside the numbers). Everything goes
to out (default <reports_dir>/sensitivity, gitignored) and to one MLflow run in the
model-sensitivity experiment (tracking.py: the server, else the local store). Exits 0, or 2 naming
the problem.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

import mlflow_runs
import training_data
import tune

EXPERIMENT = "model-sensitivity"
SAMPLE_ROWS = 500  # test rows a model without trees is explained on
BACKGROUND_ROWS = 100  # train rows the permutation explainer's masker draws from
SEED = 0
TREE_LIBRARIES = ("lightgbm", "xgboost")
ADD_UP_TOLERANCE = 1e-6  # in probability
SAME_PROBABILITY_TOLERANCE = 1e-9
SHOWN = 15  # inputs in the global charts
EXAMPLE_KINDS = {"true_alarm": "surest true alarm", "false_alarm": "surest false alarm",
                 "missed_surge": "missed surge with the lowest probability"}


class SensitivityError(ValueError):
    """A problem with the inputs, named for the user."""


def selected(selection: dict) -> tuple[dict, bool]:
    """The ranking entry of the winner, or of the best candidate when there is none, and whether it won."""
    ranking = selection.get("ranking") or []
    if not ranking:
        raise SensitivityError("selection.json ranks no candidate; run selection.py first")
    winner = selection.get("winner")
    name = winner or ranking[0]["model"]
    entry = next((record for record in ranking if record["model"] == name), None)
    if entry is None:
        raise SensitivityError(f"selection.json names {name} as the winner but does not rank it")
    return entry, winner is not None


def fitted_model(saved):
    """The fitted estimator inside a run's saved model (train.with_threshold: a FixedThresholdClassifier
    around a FrozenEstimator around it)."""
    inner = getattr(saved, "estimator", saved)
    return getattr(inner, "estimator", inner)


def is_tree_model(estimator) -> bool:
    return type(estimator).__module__.split(".")[0] in TREE_LIBRARIES


def explain(saved, rows: pd.DataFrame, inputs: list[str], background: pd.DataFrame, seed: int = SEED,
            sample_rows: int = SAMPLE_ROWS):
    """SHAP values, in log-odds, of the saved model's surge probability on rows; returns the explanation,
    the rows it explains (all of them for a tree model, else a seeded sample) and the method's name."""
    import shap

    estimator = fitted_model(saved)
    if is_tree_model(estimator):
        explanation = shap.TreeExplainer(estimator)(rows[inputs])
        if explanation.values.ndim == 3:  # one set of values per class: keep the surge class
            explanation = explanation[:, :, 1]
        return explanation, rows, "TreeExplainer, exact, every row"
    picked = rows if len(rows) <= sample_rows else rows.sample(sample_rows, random_state=seed).sort_index()
    back = background[inputs]
    back = back if len(back) <= BACKGROUND_ROWS else back.sample(BACKGROUND_ROWS, random_state=seed)

    def probability(values) -> np.ndarray:
        return saved.predict_proba(pd.DataFrame(values, columns=inputs))[:, 1]

    explainer = shap.PermutationExplainer(probability, shap.maskers.Independent(back, max_samples=len(back)),
                                          link=shap.links.logit, seed=seed)
    explanation = explainer(picked[inputs], silent=True)
    return explanation, picked, f"PermutationExplainer, {len(picked)} test rows drawn with seed {seed}"


def add_up_gap(explanation, probability) -> float:
    """The largest gap between the model's probability and the one a row's SHAP values plus the base value
    give back. Compared as probabilities: XGBoost's are 32-bit, and near 0 or 1 their log-odds would
    magnify that rounding."""
    values = np.asarray(explanation.values, dtype="float64")
    base = np.asarray(explanation.base_values, dtype="float64").reshape(len(values), -1)[:, -1]
    given_back = 1 / (1 + np.exp(-(values.sum(axis=1) + base)))
    return float(np.abs(given_back - np.asarray(probability, dtype="float64")).max())


def importance_table(explanation, inputs: list[str]) -> pd.DataFrame:
    """Per input: rank, mean absolute SHAP value, share of the total, direction and missing share."""
    values = np.asarray(explanation.values, dtype="float64")
    data = pd.DataFrame(np.asarray(explanation.data, dtype="float64"), columns=inputs)
    mean_abs = np.abs(values).mean(axis=0)
    total = float(mean_abs.sum())
    rows = []
    for j, name in enumerate(inputs):
        known = data[name].notna().to_numpy()
        x, v = data[name].to_numpy()[known], values[known, j]
        varies = known.sum() > 2 and np.ptp(x) > 0 and np.ptp(v) > 0
        rows.append({"input": name, "mean_abs_shap": float(mean_abs[j]),
                     "share": float(mean_abs[j]) / total if total else float("nan"),
                     "direction_rank_correlation": float(pd.Series(x).corr(pd.Series(v), method="spearman"))
                     if varies else float("nan"),
                     "missing_share": float(1 - known.mean())})
    table = pd.DataFrame(rows).sort_values(["mean_abs_shap", "input"], ascending=[False, True], ignore_index=True)
    table.insert(0, "rank", range(1, len(table) + 1))
    return table


def direction(correlation: float) -> str:
    if not correlation == correlation:  # NaN: the input or its values never vary
        return "no direction (constant)"
    if correlation >= 0.3:
        return "higher value, higher forecast"
    if correlation <= -0.3:
        return "higher value, lower forecast"
    return "mixed"


def examples(rows: pd.DataFrame) -> dict[str, int]:
    """Positions in rows (with probability, prediction and label) of one row of each EXAMPLE_KINDS; a kind
    with no row is left out. Ties go to the earlier date, then the unit."""
    frame = rows.reset_index(drop=True).assign(position=range(len(rows)))
    picks = {}
    for kind, mask, lowest in (("true_alarm", (frame["prediction"] == 1) & (frame["label"] == 1), False),
                               ("false_alarm", (frame["prediction"] == 1) & (frame["label"] == 0), False),
                               ("missed_surge", (frame["prediction"] == 0) & (frame["label"] == 1), True)):
        part = frame[mask]
        if len(part):
            first = part.sort_values(["probability", "date", "unit"], ascending=[lowest, True, True]).iloc[0]
            picks[kind] = int(first["position"])
    return picks


def _pyplot():
    import matplotlib

    matplotlib.use("Agg")  # PNG files, no screen
    import matplotlib.pyplot as plt

    return plt


def _save(plt, path: Path) -> Path:
    plt.gcf().savefig(path, dpi=150, bbox_inches="tight")
    plt.close("all")
    return path


def shap_charts(explanation, rows: pd.DataFrame, picks: dict[str, int], model: str, out: Path) -> list[Path]:
    """The bar and beeswarm charts of the global importance and one waterfall chart per example."""
    import shap

    plt = _pyplot()
    files = []
    for name, draw in (("shap_bar.png", shap.plots.bar), ("shap_beeswarm.png", shap.plots.beeswarm)):
        plt.figure()
        draw(explanation, max_display=SHOWN, show=False)
        plt.title(f"{model}: SHAP values in log-odds, {len(rows):,} test rows")
        files.append(_save(plt, out / name))
    for kind, position in picks.items():
        row = rows.iloc[position]
        plt.figure()
        shap.plots.waterfall(explanation[position], max_display=12, show=False)
        plt.title(f"{EXAMPLE_KINDS[kind]}: {row['unit']}, {pd.Timestamp(row['date']).date()}, "
                  f"probability {row['probability']:.3f}, surge {int(row['label'])}")
        files.append(_save(plt, out / f"shap_example_{kind}.png"))
    return files


def example_rows(explanation, rows: pd.DataFrame, picks: dict[str, int], inputs: list[str], top: int = 5) -> list[dict]:
    """Each example's day, probability, outcome and the inputs with the largest SHAP values, in log-odds."""
    records = []
    for kind, position in picks.items():
        row, values = rows.iloc[position], np.asarray(explanation.values[position], dtype="float64")
        order = sorted(range(len(inputs)), key=lambda j: (-abs(values[j]), inputs[j]))[:top]
        records.append({"kind": kind, "unit": row["unit"], "date": str(pd.Timestamp(row["date"]).date()),
                        "probability": float(row["probability"]), "prediction": int(row["prediction"]),
                        "label": int(row["label"]),
                        "top_inputs": [{"input": inputs[j], "value": _number(row[inputs[j]]),
                                        "shap": round(float(values[j]), 6)} for j in order]})
    return records


def _number(value):
    return None if pd.isna(value) else float(value)


def _f1_share(share: dict) -> float:
    return share["f1"] if share["f1"] is not None else -1.0


def hyperparameters(tuning, out: Path) -> tuple[list[dict], dict, list[Path]]:
    """M6's studies: the hp_importance.csv rows, a summary per model and the charts written."""
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)  # load_study's "A new study created" lines
    plt = _pyplot()
    stored = {}
    importance_file = tuning.study_dir / "importance.csv"
    if importance_file.is_file():
        with open(importance_file, encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                stored[(record["model"], record["setting"])] = record
    rows, summary, files = [], {}, []
    for name in tuning.models:
        path = tuning.study_dir / f"{name}.json"
        if not path.is_file():
            summary[name] = {"study": None, "note": f"no {path.name} in {tuning.study_dir}: run tune.py (#109)"}
            continue
        study = tune.load_study(path)
        shares = tune.importances(study, tuning.seed)
        order = sorted(shares, key=lambda key: (-_f1_share(shares[key]), key))
        top = order[:2]
        for rank, key in enumerate(order, start=1):
            rows.append({"model": name, "setting": key, "rank_f1": rank, "importance_f1": shares[key]["f1"],
                         "importance_log_loss": shares[key]["log_loss"]})
        compared = compare_with_stored(name, shares, order, stored)
        _, axis = plt.subplots(figsize=(7, 0.45 * len(order) + 1.5))
        positions = np.arange(len(order))
        for offset, score, label in ((-0.2, "f1", "fold F1 (the objective)"), (0.2, "log_loss", "inner log loss")):
            axis.barh(positions + offset, [shares[key][score] or 0 for key in order], height=0.4, label=label)
        axis.set_yticks(positions, order)
        axis.invert_yaxis()
        axis.set_xlabel("share of the variation across trials (fANOVA, seeded)")
        axis.set_title(f"{name}: tuning setting importance, {len(study.trials)} trials")
        axis.legend(loc="lower right")
        files.append(_save(plt, out / f"hp_importance_{name}.png"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", optuna.exceptions.ExperimentalWarning)
            axes = np.ravel(optuna.visualization.matplotlib.plot_slice(study, params=top))
        axes[0].figure.set_size_inches(5.5 * len(axes) + 1, 4.5)
        axes[0].set_ylabel("fold F1 (mean over the trial seeds)")
        axes[0].figure.suptitle(f"{name}: fold F1 of every trial against its top 2 settings")
        files.append(_save(plt, out / f"hp_slice_{name}.png"))
        summary[name] = {"study": path.name, "trials": len(study.trials), "top_settings": top,
                         "shares": shares, "importance_csv": compared}
    return rows, summary, files


def compare_with_stored(name: str, shares: dict, order: list[str], stored: dict) -> dict | None:
    """How the shares compare with importance.csv's: the largest difference and whether the settings
    rank in the same order by F1 importance; None when importance.csv has no row for the model."""
    rows = {setting: record for (model, setting), record in stored.items() if model == name}
    if not rows:
        return None
    gaps = [abs(shares[key][score] - float(rows[key][f"importance_{score}"]))
            for key in shares if key in rows for score in ("f1", "log_loss")
            if shares[key][score] is not None and rows[key][f"importance_{score}"] != ""]
    stored_order = sorted(rows, key=lambda key: (-float(rows[key]["importance_f1"] or -1), key))
    return {"max_difference": max(gaps) if gaps else None, "same_order": stored_order == order}


def _write_csv(rows: list[dict], path: Path) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def report_text(summary: dict, table: pd.DataFrame) -> str:
    """report.md: what was explained, the checks, the inputs that move the forecast and the tuning settings."""
    shap_part = summary["shap"]
    lines = [
        f"# Sensitivity analysis (#113): {summary['model']}", "",
        f"{summary['model']} is {'the winner' if summary['selected'] else 'the best candidate (no winner)'} of "
        f"selection {summary['selection_id']}, on data version {summary['data_version']}. "
        f"{shap_part['method']}: {shap_part['rows']:,} scored test rows, {shap_part['first_day']} to {shap_part['last_day']}.",
        "",
        f"Checks: the saved model gives back its run's test probabilities (largest difference "
        f"{summary['checks']['probability_max_difference']:.1e}); each row's SHAP values plus the base value give back "
        f"its probability (largest gap {summary['checks']['shap_add_up_max_gap']:.1e}).",
        "",
        "## Inputs that move the forecast", "",
        "SHAP splits each forecast's log-odds among the inputs. These are associations, not causes: a large share "
        "means the model leans on that input in the test data, not that the input causes 311 surges, and inputs "
        "that move together (alert counts and forecast snowfall, the 311 lags of one storm group) share credit in "
        "ways the real world need not.", "",
        "| rank | input | share of mean abs SHAP | direction | missing |", "|---|---|---|---|---|",
    ]
    for record in table.head(10).itertuples(index=False):
        lines.append(f"| {record.rank} | {record.input} | {_pct(record.share)} | "
                     f"{direction(record.direction_rank_correlation)} | {_pct(record.missing_share)} |")
    lines += ["", "## Example days", ""]
    for record in shap_part["examples"]:
        parts = ", ".join(f"{item['input']} {item['shap']:+.3f}" for item in record["top_inputs"][:3])
        lines.append(f"- {EXAMPLE_KINDS[record['kind']]}: {record['unit']} on {record['date']}, probability "
                     f"{record['probability']:.3f}, surge {record['label']}; largest log-odds pushes: {parts}.")
    lines += ["", "## Tuning settings (M6's studies, fANOVA, seeded)", ""]
    for name, part in summary["hyperparameters"].items():
        if part["study"] is None:
            lines.append(f"- {name}: {part['note']}.")
            continue
        top = ", ".join(f"{key} {_pct(part['shares'][key]['f1'] or 0)}" for key in part["top_settings"])
        compared = part["importance_csv"]
        against = ("no importance.csv row to compare" if compared is None else
                   f"importance.csv differs by at most {compared['max_difference']:.4f}, "
                   f"{'same' if compared['same_order'] else 'a different'} order of settings")
        lines.append(f"- {name} ({part['trials']} trials): top settings for the fold F1: {top}; {against}.")
    return "\n".join(lines) + "\n"


def load_inputs(settings, selection_path: Path):
    """Everything the analysis needs, checked: (selection, its entry, whether it won, the saved model, its
    inputs, the scored test rows with the run's probability, prediction and label, the train rows)."""
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    version = training_data.data_version(settings.dvc_lock)
    if selection.get("data_version") != version:
        raise SensitivityError(f"{selection_path} comes from data version {selection.get('data_version')}, but "
                               f"this checkout's is {version}; run selection.py again")
    entry, won = selected(selection)
    run_dir = Path(settings.runs_dir) / entry["run_folder"]
    if not (run_dir / "model.joblib").is_file():
        raise SensitivityError(f"{run_dir} has no model.joblib; run selection.py again")
    saved = joblib.load(run_dir / "model.joblib")
    inputs = list(json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["inputs"])
    table, _ = training_data.load_table(settings)
    test = table[(table["split"] == "test") & table["scored"]].reset_index(drop=True)
    if test.empty:
        raise SensitivityError("the test split has no scored rows")
    stored = pd.read_parquet(run_dir / "predictions.parquet")
    stored = stored[stored["split"] == "test"][["unit", "date", "probability", "prediction", "label"]]
    stored = stored.assign(date=pd.to_datetime(stored["date"]))
    test = test.drop(columns=["label"]).assign(date=pd.to_datetime(test["date"])).merge(
        stored, on=["unit", "date"], how="left", validate="one_to_one")
    if test["probability"].isna().any() or len(stored) != len(test):
        raise SensitivityError(f"the test rows differ from {run_dir.name}'s predictions; run selection.py again")
    gap = float(np.abs(saved.predict_proba(test[inputs])[:, 1] - test["probability"].to_numpy()).max())
    if gap > SAME_PROBABILITY_TOLERANCE:
        raise SensitivityError(f"{run_dir.name}'s model gives test probabilities up to {gap:.2e} away from its "
                               "predictions.parquet; run selection.py again")
    return selection, entry, won, saved, inputs, test, table[table["split"] == "train"], gap


def run(settings, tuning, selection_path: Path, out: Path, experiment: str = EXPERIMENT, progress=print) -> dict:
    """The analysis (see the module docstring); returns sensitivity.json's content."""
    selection, entry, won, saved, inputs, test, train_rows, probability_gap = load_inputs(settings, selection_path)
    model = entry["model"]
    out.mkdir(parents=True, exist_ok=True)
    tags = {"purpose": "sensitivity analysis (#113)", "model": model, "selected": "winner" if won else "best candidate",
            "selection_id": selection.get("selection_id"), "data_version": selection["data_version"],
            "model_run_folder": entry["run_folder"], "model_mlflow_run_id": entry.get("mlflow_run_id")}
    with mlflow_runs.tracked_run(dataclasses.replace(settings, mlflow_experiment=experiment),
                                 f"sensitivity-{model}", tags):
        explanation, explained, method = explain(saved, test, inputs, train_rows)
        gap = add_up_gap(explanation, saved.predict_proba(explained[inputs])[:, 1])
        if is_tree_model(fitted_model(saved)) and gap > ADD_UP_TOLERANCE:
            raise SensitivityError(f"the SHAP values miss the model's probability by up to {gap:.2e}")
        progress(f"{model}: {method}, SHAP values give back the probability within {gap:.1e}", flush=True)
        table = importance_table(explanation, inputs)
        explained = explained.reset_index(drop=True)
        picks = examples(explained)
        files = [_write_csv(table.to_dict("records"), out / "shap_importance.csv")]
        shown = example_rows(explanation, explained, picks, inputs)
        files.append(_write_csv([{"kind": r["kind"], "unit": r["unit"], "date": r["date"], "probability": r["probability"],
                                  "prediction": r["prediction"], "label": r["label"],
                                  **{f"top{k + 1}": f"{item['input']} {item['shap']:+.4f}"
                                     for k, item in enumerate(r["top_inputs"])}} for r in shown],
                                out / "shap_examples.csv"))
        files += shap_charts(explanation, explained, picks, model, out)
        hp_rows, hp_summary, hp_files = hyperparameters(tuning, out)
        if hp_rows:
            files.append(_write_csv(hp_rows, out / "hp_importance.csv"))
        files += hp_files
        summary = {
            "model": model, "selected": won, "selection_id": selection.get("selection_id"),
            "data_version": selection["data_version"], "run_folder": entry["run_folder"],
            "checks": {"probability_max_difference": probability_gap, "shap_add_up_max_gap": gap},
            "shap": {"method": method, "rows": len(explained), "units": "log-odds of the surge probability",
                     "first_day": str(explained["date"].min().date()), "last_day": str(explained["date"].max().date()),
                     "base_value": float(np.ravel(explanation.base_values)[0]),
                     "importance": table.to_dict("records"), "examples": shown},
            "hyperparameters": hp_summary,
        }
        (out / "sensitivity.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        (out / "report.md").write_text(report_text(summary, table), encoding="utf-8")
        files += [out / "sensitivity.json", out / "report.md"]
        metrics = {"probability_max_difference": probability_gap, "shap_add_up_max_gap": gap,
                   **{f"shap_share_{record['input']}": record["share"] for record in table.to_dict("records")}}
        for name, part in hp_summary.items():
            for key in part.get("top_settings", []):
                if part["shares"][key]["f1"] is not None:
                    metrics[f"hp_importance_f1_{name}_{key}"] = part["shares"][key]["f1"]
        mlflow_runs.log_summary({"method": method, "rows_explained": len(explained), "inputs": len(inputs),
                                 "seed": SEED, "studies": [n for n, p in hp_summary.items() if p["study"]]},
                                metrics, {"top_input": table.iloc[0]["input"]}, files)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG))
    parser.add_argument("--tuning", default=str(tune.DEFAULT_TUNING))
    parser.add_argument("--selection", help="selection.json (default <reports_dir>/selection.json)")
    parser.add_argument("--out", help="output folder (default <reports_dir>/sensitivity)")
    args = parser.parse_args(argv)
    try:
        settings = training_data.load_settings(args.config)
        tuning = tune.load_tuning(args.tuning)
        reports = Path(settings.reports_dir)
        summary = run(settings, tuning, Path(args.selection) if args.selection else reports / "selection.json",
                      Path(args.out) if args.out else reports / "sensitivity")
    except (ValueError, FileNotFoundError, KeyError) as exc:  # SensitivityError and SettingsError are ValueErrors
        print(f"sensitivity: {exc}", file=sys.stderr)
        return 2
    top = summary["shap"]["importance"][:3]
    print(f"{summary['model']}: top inputs " + ", ".join(f"{r['input']} {_pct(r['share'])}" for r in top))
    for name, part in summary["hyperparameters"].items():
        print(f"{name}: " + (f"top settings {', '.join(part['top_settings'])}" if part["study"] else part["note"]))
    print(f"wrote {Path(args.out) if args.out else Path(settings.reports_dir) / 'sensitivity'}; logged to MLflow "
          f"({mlflow_runs.where(settings)}, experiment {EXPERIMENT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
