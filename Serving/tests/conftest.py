"""Shared test setup (#161): no network, the repository paths, and a tiny real model.

Tests use fixture files and fakes only, never the network. CI also runs the container with `--network none`;
the guard below is the same as .github/component-template/tests/conftest.py: Unix sockets and loopback stay
allowed. The tests run in the image (WORKDIR /app, tests at /app/tests) and in a checkout (Serving/tests),
so every path is found from either layout.

The model under test is real: `model_dir` trains a small LightGBM model on synthetic rows with the
repository's own wrapper (Model-Development/scripts/models.py), saves it the way train.py does (a
FixedThresholdClassifier over a FrozenEstimator) and writes its metadata.json with scripts/local_model.py,
so the service loads it by the real path. Nothing under test is mocked.
"""

from __future__ import annotations

import copy
import json
import shutil
import socket
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent


def find(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"none of {[str(c) for c in candidates]} exists")


# In the image HERE.parent is /app; in a checkout HERE.parents[1] is the repository root.
CONTRACTS = find(HERE.parent / "contracts", HERE.parents[1] / "docs" / "contracts")
SCHEMAS = CONTRACTS / "schemas"
FIXTURES = CONTRACTS / "fixtures"
LOCK = find(HERE.parent / "Model-Development" / "requirements.lock",
            HERE.parents[1] / "Model-Development" / "requirements.lock")
REQUIREMENTS = find(HERE.parent / "Serving" / "requirements.txt", HERE.parent / "requirements.txt")
for folder in (find(HERE.parent / "Model-Development" / "scripts", HERE.parents[1] / "Model-Development" / "scripts"),
               find(HERE.parent / "scripts")):
    sys.path.insert(0, str(folder))

import local_model  # noqa: E402  (Serving/scripts, on the path above)
import models  # noqa: E402  (Model-Development/scripts, on the path above)

from serving import config  # noqa: E402
from serving.app import create_app  # noqa: E402
from serving.contract import Contract  # noqa: E402

MLFLOW_RUN_ID = "48adb4f5c1f74cccb34437290c6307cf"
THRESHOLD = 0.0959946834206164
MODEL_VERSION = "7"


class NetworkBlocked(RuntimeError):
    pass


LOOPBACK = {"127.0.0.1", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect
    real_lookup = socket.getaddrinfo

    def guarded_connect(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None):
            return real_connect(sock, address)
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return real_connect(sock, address)
        raise NetworkBlocked(f"test tried to connect to {address!r}; use a fixture file or a fake instead")

    def blocked_lookup(host, *args, **kwargs):
        if host in LOOPBACK:
            return real_lookup(host, *args, **kwargs)
        raise NetworkBlocked(f"test tried to look up {host!r}; use a fixture file or a fake instead")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)


@pytest.fixture(scope="session")
def contract() -> Contract:
    return Contract(SCHEMAS)


@pytest.fixture(scope="session")
def request_body() -> dict:
    return json.loads((FIXTURES / "predict" / "request.json").read_text(encoding="utf-8"))


def synthetic_rows(contract: Contract, n: int = 300, seed: int = 0) -> pd.DataFrame:
    """n training-like rows: every input of the schema in its dtype, some nulls where nulls are allowed
    (the *_rel* inputs), dates over 2018 to 2020, and a 0 or 1 label that depends on a few inputs."""
    rng = np.random.default_rng(seed)
    columns: dict[str, np.ndarray] = {}
    for name in contract.features:
        if contract.dtypes[name] == "int64":
            columns[name] = rng.integers(0, 4, n)
        else:
            values = rng.random(n) * 10
            if "_rel" in name:
                values[rng.random(n) < 0.1] = np.nan
            columns[name] = values
    rows = pd.DataFrame(columns)
    rows["date"] = pd.to_datetime("2018-01-01") + pd.to_timedelta(rng.integers(0, 3 * 365, n), unit="D")
    score = rows["wx_precip_mm"] + rows["tree_rel3"].fillna(5) + rng.normal(0, 2, n)
    rows["label"] = (score > np.quantile(score, 0.7)).astype(int)
    return rows


def train_model(contract: Contract, features: tuple[str, ...], folder: Path) -> models.LightGBMModel:
    """Train with the repository's LightGBM wrapper, save folder/model.joblib as train.py does and write
    folder/metadata.json with local_model.py. Returns the wrapper, whose predict_proba is the reference."""
    from sklearn.frozen import FrozenEstimator
    from sklearn.model_selection import FixedThresholdClassifier

    rows = synthetic_rows(contract)
    wrapper = models.LightGBMModel(
        list(features), (2019, 2020), learning_rate=0.1, max_rounds=40, stopping_rounds=5, num_leaves=4,
        min_child_samples=5, reg_lambda=1.0, subsample=1.0, subsample_freq=0, colsample_bytree=1.0, seed=0)
    wrapper.fit(rows, rows["label"], np.ones(len(rows)))
    saved = FixedThresholdClassifier(FrozenEstimator(wrapper.estimator), threshold=THRESHOLD)
    saved.fit(rows[list(features)], rows["label"])

    run = folder.parent / (folder.name + "-run")
    run.mkdir(parents=True)
    joblib.dump(saved, run / "model.joblib")
    (run / "run.json").write_text(json.dumps({
        "model": "lightgbm", "run_id": "ci-lightgbm", "threshold": THRESHOLD, "data_version": "sha256:" + "a" * 64,
        "mlflow": {"run_id": MLFLOW_RUN_ID}}), encoding="utf-8")
    saved_repo = local_model.REPO
    local_model.REPO = folder.parent / "not-the-repo"
    try:
        assert local_model.main([str(run), str(folder)]) == 0
    finally:
        local_model.REPO = saved_repo
    return wrapper


@pytest.fixture(scope="session")
def trained(tmp_path_factory, contract):
    """(model folder, the wrapper that was trained) for a model on all 31 inputs."""
    folder = tmp_path_factory.mktemp("full") / "model"
    return folder, train_model(contract, contract.features, folder)


@pytest.fixture(scope="session")
def model_dir(trained) -> Path:
    return trained[0]


@pytest.fixture(scope="session")
def wrapper(trained):
    return trained[1]


@pytest.fixture(scope="session")
def subset_model_dir(tmp_path_factory, contract) -> Path:
    """A model that takes one input fewer than the contract lists."""
    folder = tmp_path_factory.mktemp("subset") / "model"
    train_model(contract, contract.features[:-1], folder)
    return folder


@pytest.fixture
def make_settings(tmp_path):
    def make(routes: str = "model", **overrides) -> config.Settings:
        frontend = tmp_path / "frontend"
        frontend.mkdir(exist_ok=True)
        values = dict(routes=routes, model_dir=tmp_path / "no-model", model_version=MODEL_VERSION,
                      forecasts_root=str(tmp_path / "forecasts"), frontend_dir=frontend, schemas_dir=SCHEMAS)
        values.update(overrides)
        return config.Settings(**values)
    return make


@pytest.fixture
def make_client(make_settings):
    """A TestClient (its lifespan runs, so the model loads) for the given routes and settings."""
    opened: list[TestClient] = []

    def make(routes: str = "model", session_factory=None, **overrides) -> TestClient:
        kwargs = {"session_factory": session_factory} if session_factory else {}
        client = TestClient(create_app(make_settings(routes, **overrides), **kwargs))
        client.__enter__()
        opened.append(client)
        return client
    yield make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def served(make_client, model_dir):
    """A client on a model-mode service with the real model loaded."""
    return make_client("model", model_dir=model_dir)


@pytest.fixture
def log_lines():
    """The messages the service logs while a test runs."""
    import logging

    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Capture()
    logging.getLogger("serving").addHandler(handler)
    yield lines
    logging.getLogger("serving").removeHandler(handler)


@pytest.fixture
def body(request_body) -> dict:
    return copy.deepcopy(request_body)


def copy_model(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    return target
