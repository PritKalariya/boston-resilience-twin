"""Load the served model, after checking its bytes (ADR-037).

The model folder holds `model.joblib` and `metadata.json`, the file the registry push writes beside the
model (Model-Development/scripts/registry.py, build_push). Unpickling runs arbitrary code, so every file
metadata.json lists is hashed first and `joblib.load` is only reached when they all match. Any problem
leaves the model unloaded with a reason, which /health reports as a 503.

metadata.json lists more files than the image ships (run.json, metrics.json, the selection report): a
listed file that is not in the folder is never read, so it is not checked. `model.joblib` must be listed,
present and matching.

After loading, the threshold in metadata.json must equal the one embedded in the model, exactly.

Scoring is the path of step_reload_check.py: the saved estimator's predict_proba on a frame of its own
input columns, probability = column 1. The saved file holds a scikit-learn FixedThresholdClassifier over a
LightGBM model, so loading it imports scikit-learn and lightgbm only, none of the training code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("serving")

MODEL_FILE = "model.joblib"
METADATA_FILE = "metadata.json"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ModelUnavailable(Exception):
    """The model cannot be served; the message is the reason /health returns."""


@dataclass(frozen=True)
class LoadedModel:
    estimator: object
    features: tuple[str, ...]
    threshold: float
    model_version: str
    run_id: str  # the MLflow run id (metadata.json mlflow_run_id), the contract's model_run_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_metadata(model_dir: Path) -> dict:
    path = model_dir / METADATA_FILE
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModelUnavailable(f"{path} is missing") from None
    except (OSError, ValueError) as exc:
        raise ModelUnavailable(f"{path} cannot be read as JSON: {exc}") from None
    if not isinstance(metadata, dict):
        raise ModelUnavailable(f"{path} is not a JSON object")
    return metadata


def verify_files(model_dir: Path, metadata: dict) -> None:
    """Hash every listed file that is in the folder; model.joblib must be listed, present and matching."""
    files = metadata.get("files")
    if not isinstance(files, dict) or MODEL_FILE not in files:
        raise ModelUnavailable(f"metadata.json lists no sha256 for {MODEL_FILE}")
    for name, expected in sorted(files.items()):
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ModelUnavailable(f"metadata.json lists a file name that is not a plain name: {name!r}")
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise ModelUnavailable(f"metadata.json gives {name} no sha256:<64 hex> value")
        path = model_dir / name
        if not path.is_file():
            if name == MODEL_FILE:
                raise ModelUnavailable(f"{path} is missing")
            continue
        actual = _sha256(path)
        if actual != expected:
            raise ModelUnavailable(f"{name} does not match the sha256 in metadata.json (got {actual})")


def load_model(model_dir: Path, model_version: str, run_id_pattern: str, version_pattern: str) -> LoadedModel:
    """The verified model, or ModelUnavailable saying why not."""
    model_dir = Path(model_dir)
    metadata = read_metadata(model_dir)
    verify_files(model_dir, metadata)  # before any unpickling

    run_id = metadata.get("mlflow_run_id")
    if not isinstance(run_id, str) or not re.fullmatch(run_id_pattern, run_id):
        raise ModelUnavailable(f"metadata.json mlflow_run_id {run_id!r} does not match {run_id_pattern}")
    threshold = metadata.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ModelUnavailable(f"metadata.json threshold {threshold!r} is not a number from 0 to 1")
    if not re.fullmatch(version_pattern, model_version or ""):
        raise ModelUnavailable(f"MODEL_VERSION {model_version!r} does not match {version_pattern}")

    import joblib

    try:
        estimator = joblib.load(model_dir / MODEL_FILE)
        features = tuple(str(name) for name in estimator.feature_names_in_)
        if not callable(getattr(estimator, "predict_proba", None)):
            raise TypeError("the saved object has no predict_proba")
    except Exception as exc:  # a pickle can fail in any way
        raise ModelUnavailable(f"{MODEL_FILE} cannot be loaded: {type(exc).__name__}: {exc}") from None
    # The saved model carries the threshold it was trained with; metadata.json must say the same one, exactly,
    # or the flag the service answers would differ from the flag model.predict gives.
    embedded = getattr(estimator, "threshold", None)
    if isinstance(embedded, bool) or not isinstance(embedded, (int, float)):
        raise ModelUnavailable(f"{MODEL_FILE} has no embedded threshold (a FixedThresholdClassifier has one); "
                               f"metadata.json says {threshold!r}")
    if embedded != threshold:
        raise ModelUnavailable(f"metadata.json threshold {threshold!r} differs from the model's embedded "
                               f"threshold {embedded!r}")
    log.info(json.dumps({"event": "model_loaded", "model_version": model_version, "features": len(features)}))
    return LoadedModel(estimator, features, float(threshold), model_version, run_id)


def probabilities(model: LoadedModel, frame) -> np.ndarray:
    """Surge probabilities, one per row of frame, unrounded (step_reload_check.py's call)."""
    return np.asarray(model.estimator.predict_proba(frame[list(model.features)]), dtype="float64")[:, 1]
