"""MLflow tracking for every training run (#107). Named tracking.py, never mlflow.py, which would
hide the mlflow package from every script in this folder.

Where runs go: the MLflow server that MLFLOW_TRACKING_URI names (M1, #104), or else a local store in
mlflow_store (training.csv): Model-Development/mlruns/mlflow.db, an SQLite database, with the
artifacts beside it in artifacts/ (gitignored). MLflow 3.7 and later default to SQLite too, and
3.13 and later refuse the old file store. The image has mlflow-skinny, the client; to browse the
local store, install the full package (pip install mlflow==3.18.0) and run
    mlflow ui --backend-store-uri sqlite:///Model-Development/mlruns/mlflow.db

Each train.py run is one MLflow run, logged explicitly (no autolog). Inside an active run, such as
a selection run (#107), it is logged as a child of that run.
- tags: the model, data version, git commit, the run folder's id and the split windows;
- params: the threshold and its rule, what the model chose on the inner folds (chosen_*), every
  parameter it used (param_*) with the tuned ones and the tuned file's sha256, the number of
  inputs, and the training settings;
- metrics: every metric of both splits (validation_f1, test_pr_auc, ...), the inner-fold log loss,
  and the costs: train_time_s, score_time_evening_ms_median and _p95, model_size_bytes. A metric
  that cannot be computed is left out and named in the tag undefined_metrics;
- artifacts: the run folder's files, inputs.json (the feature list) and, for a candidate, the
  model with its threshold as an MLflow scikit-learn model (FixedThresholdClassifier around the
  frozen fitted model, so predict gives the surge flag and predict_proba the probability). It is
  saved with cloudpickle: MLflow 3.18's default, skops, refuses the LightGBM and XGBoost types, and
  mlflow-skinny does not ship it. Load these models only from this project's own store.

Telemetry to MLflow and MLflow's import-time hint for coding agents are off unless the caller's
environment turns them on. mlflow is imported on first use: the import takes seconds, and
importing train.py (as the workflow steps do) should not pay for it.
"""

from __future__ import annotations

import json
import logging
import os
import warnings
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import sklearn

os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "true")
# log_model loads the model back to check its input example, which prints a download bar per model
os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")


def _cloudpickle_caution(record: logging.LogRecord) -> bool:
    """False for MLflow's caution about cloudpickle, a deliberate choice documented above."""
    return not record.getMessage().startswith("Saving scikit-learn models in the pickle or cloudpickle")


def _mlflow():
    """The mlflow package, imported and quietened on first use."""
    import mlflow
    import mlflow.sklearn  # noqa: F401  (the flavor log_model uses)

    for name in ("mlflow", "alembic"):  # store setup and migrations print INFO lines on every run
        logging.getLogger(name).setLevel(logging.WARNING)
    flavor = logging.getLogger("mlflow.sklearn")
    if _cloudpickle_caution not in flavor.filters:
        flavor.addFilter(_cloudpickle_caution)
    return mlflow


SERVER_VARIABLE = "MLFLOW_TRACKING_URI"
MODEL_PACKAGES = {"lightgbm": "lightgbm", "xgboost": "xgboost"}  # model name -> the library its saved model needs


def tracking_uri(settings) -> str:
    """The server MLFLOW_TRACKING_URI names, or the local SQLite store in mlflow_store."""
    return os.environ.get(SERVER_VARIABLE) or f"sqlite:///{(Path(settings.mlflow_store) / 'mlflow.db').as_posix()}"


def where(settings) -> str:
    """Where runs go, for run.json and the console; a server URI is not repeated, since it may hold a token."""
    return f"server ({SERVER_VARIABLE})" if os.environ.get(SERVER_VARIABLE) else f"local store {Path(settings.mlflow_store).as_posix()}"


@contextmanager
def tracked_run(settings, run_name: str, tags: dict):
    """An MLflow run in the mlflow_experiment of settings, nested when another run is active. A
    local store gets its experiment with the artifacts beside the database."""
    mlflow = _mlflow()
    server = os.environ.get(SERVER_VARIABLE)
    mlflow.set_tracking_uri(tracking_uri(settings))
    if server is None:
        # set_tracking_uri also writes MLFLOW_TRACKING_URI into this process, which would make the
        # local store look like a server to the next check; MLflow keeps the URI without it
        os.environ.pop(SERVER_VARIABLE, None)
        if mlflow.get_experiment_by_name(settings.mlflow_experiment) is None:
            store = Path(settings.mlflow_store)
            store.mkdir(parents=True, exist_ok=True)
            mlflow.create_experiment(settings.mlflow_experiment,
                                     artifact_location=(store / "artifacts").resolve().as_uri())
    mlflow.set_experiment(settings.mlflow_experiment)
    with mlflow.start_run(run_name=run_name, nested=mlflow.active_run() is not None,
                          tags={key: str(value) for key, value in tags.items()}) as run:
        yield run


def log_model(model, example, model_name: str) -> str:
    """Log model (a fitted scikit-learn estimator with its threshold) as this run's MLflow model and
    return its models:/ URI. example is a few rows of inputs, logged as floats so that missing values
    fit the signature. The pip requirements are pinned here: inferring them would load the model in
    a subprocess."""
    mlflow = _mlflow()
    requirements = [f"scikit-learn=={sklearn.__version__}", f"numpy=={np.__version__}"]
    if model_name in MODEL_PACKAGES:
        package = MODEL_PACKAGES[model_name]
        requirements.append(f"{package}=={__import__(package).__version__}")
    with warnings.catch_warnings():
        # the output is the 0 or 1 surge flag, never missing, so MLflow's integer-column hint does not apply
        warnings.filterwarnings("ignore", message="Hint: Inferred schema contains integer column")
        info = mlflow.sklearn.log_model(model, name="model", input_example=example.astype("float64"),
                                        serialization_format="cloudpickle", pip_requirements=requirements)
    return info.model_uri


def _flat(value) -> str:
    return json.dumps(value) if isinstance(value, (list, tuple, dict)) else str(value)


def params(run: dict) -> dict[str, str]:
    """The run's params: threshold, choices, parameters and settings, as MLflow strings."""
    out = {"model": run["model"], "threshold": run["threshold"], "threshold_rule": run["threshold_rule"],
           "inputs": len(run["inputs"])}
    out.update({f"chosen_{name}": value for name, value in run["model_details"].get("chosen", {}).items()})
    parameters = run["parameters"]
    if parameters:
        out.update({f"param_{name}": value for name, value in parameters["values"].items()})
        out["tuned"] = ",".join(parameters["tuned"]) or "none"
        out["tuned_sha256"] = parameters["tuned_sha256"] or "none"
    settings = run["settings"]
    for name in ("inner_fold_years", "down_weight_years", "down_weight", "missing_gap_max_points"):
        out[name] = settings[name]
    if run["model"] == "seasonal":
        out.update({name: settings[name] for name in ("seasonal_years", "seasonal_half_width_days", "seasonal_min_days")})
    return {name: _flat(value) for name, value in out.items()}


def metrics(run: dict, scores: dict) -> tuple[dict[str, float], list[str]]:
    """The run's metrics as split_metric (and the inner-fold log loss and the costs), and the names of
    those that cannot be computed, which MLflow cannot store as numbers."""
    out, undefined = {}, []
    for split, values in scores.items():
        for name, value in values.items():
            if value is None:
                undefined.append(f"{split}_{name}")
            else:
                out[f"{split}_{name}"] = float(value)
    if "inner_log_loss" in run["model_details"]:
        out["inner_log_loss"] = float(run["model_details"]["inner_log_loss"])
    costs = run["costs"]
    out["train_time_s"] = costs["train_time_s"]
    out["score_time_evening_ms_median"] = costs["score_time_evening_ms"]["median"]
    out["score_time_evening_ms_p95"] = costs["score_time_evening_ms"]["p95"]
    if costs["model_size_bytes"] is not None:
        out["model_size_bytes"] = float(costs["model_size_bytes"])
    return out, undefined


def log_summary(params: dict, metrics: dict[str, float], tags: dict, files: list[Path]) -> None:
    """Log a summary on the active run, such as a selection run's decision: params, metrics, tags and files."""
    mlflow = _mlflow()
    mlflow.log_params({name: _flat(value) for name, value in params.items()})
    mlflow.log_metrics(metrics)
    mlflow.set_tags({name: str(value) for name, value in tags.items()})
    for path in files:
        mlflow.log_artifact(str(path))


def log_run(run_dir: Path, run: dict, scores: dict) -> None:
    """Log the run's params, metrics, the names of its undefined metrics, and the run folder's files
    with inputs.json; the saved model file is left out, since log_model holds the model."""
    mlflow = _mlflow()
    mlflow.log_params(params(run))
    values, undefined = metrics(run, scores)
    mlflow.log_metrics(values)
    mlflow.set_tag("undefined_metrics", ",".join(undefined) or "none")
    mlflow.log_dict({"inputs": run["inputs"]}, "inputs.json")
    for path in sorted(Path(run_dir).iterdir()):
        if path.is_file() and path.suffix != ".joblib":
            mlflow.log_artifact(str(path))
