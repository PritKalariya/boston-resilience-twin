"""Where MLflow runs go (#104, ADR-006): the tracking server, or a local store without it.

MLFLOW_TRACKING_URI names the server: http://mlflow:5000 from a container on the compose
network, http://127.0.0.1:5000 from the host (MLFLOW_HOST_PORT in .env moves it), and the
same through an SSH tunnel on the VM (ADR-016). Unset or empty, runs go to a local store
instead, so nothing needs the server: GitHub Actions runners cannot reach it (ADR-006), and a
fresh clone may not run it. The local store is Model-Development/mlruns/ (gitignored), or the
folder a caller names, such as a workflow step's artifacts directory, the only place a step
writes (ADR-028).

MLflow 3.17 refuses its old ./mlruns file store, so the local store is a SQLite database
(mlflow.db) beside an artifacts folder: the server's backend too, so local runs can be
imported into the server later (ADR-006, Consequences). MLflow tracks runs only, never
registered models, stages or aliases (ADR-006).

    import tracking
    tracking.setup("surge-forecast")  # then mlflow.start_run(), mlflow.log_metric(), ...
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ENV = "MLFLOW_TRACKING_URI"
LOCAL_DIR = Path(__file__).resolve().parents[1] / "mlruns"  # Model-Development/mlruns
HEALTH_TIMEOUT_S = 5


def local_folder(local_dir: Path | str | None = None) -> Path:
    """The local store's folder: local_dir, or Model-Development/mlruns."""
    return (Path(local_dir) if local_dir is not None else LOCAL_DIR).resolve()


def local_uri(local_dir: Path | str | None = None) -> str:
    """The local store's database, as a SQLite URI."""
    return "sqlite:///" + (local_folder(local_dir) / "mlflow.db").as_posix()


def tracking_uri(local_dir: Path | str | None = None) -> str:
    """The server MLFLOW_TRACKING_URI names, or the local store when it is unset or empty."""
    return os.environ.get(ENV, "").strip() or local_uri(local_dir)


def check_server(uri: str, timeout: float = HEALTH_TIMEOUT_S) -> None:
    """Raise ConnectionError, saying how to log locally instead, unless uri answers /health with OK."""
    url = uri.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(64).decode("utf-8", "replace").strip()
    except OSError as exc:  # URLError, HTTPError and timeouts are all OSError
        raise ConnectionError(
            f"{ENV} is {uri}, but {url} did not answer ({exc}). Start the server with "
            f"`docker compose up -d --wait mlflow`, or unset {ENV} to log to the local store") from exc
    if body != "OK":
        raise ConnectionError(f"{url} answered {body!r}, not OK: is {uri} an MLflow tracking server?")


def setup(experiment: str, local_dir: Path | str | None = None) -> str:
    """Point MLflow at tracking_uri(local_dir) and make experiment the active one; return the URI.

    A server must answer /health first, so a stopped or wrong one fails in seconds instead of
    after MLflow's retries. In the local store a new experiment keeps its artifacts in
    <local_dir>/artifacts/, beside the database, not in the current folder, MLflow's default
    there. On the server, the server keeps them (--artifacts-destination in docker-compose.yaml).
    """
    uri = tracking_uri(local_dir)
    local = uri == local_uri(local_dir)
    if uri.startswith(("http://", "https://")):
        check_server(uri)
    if local:
        local_folder(local_dir).mkdir(parents=True, exist_ok=True)  # SQLite makes the file, not its folder

    import mlflow  # here, so the module imports without MLflow

    mlflow.set_tracking_uri(uri)
    if local and mlflow.get_experiment_by_name(experiment) is None:
        folder = local_folder(local_dir) / "artifacts" / (re.sub(r"[^A-Za-z0-9._-]+", "-", experiment).strip("-") or "experiment")
        mlflow.create_experiment(experiment, artifact_location=folder.as_uri())
    mlflow.set_experiment(experiment)
    return uri
