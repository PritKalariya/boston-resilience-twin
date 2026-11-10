"""Smoke check of MLflow tracking (#104): log one run with a parameter, a metric and an
artifact, read the three back, and print where the UI shows the run.

    docker compose run --rm --build mlflow-smoke      # against the compose server, from a container
    python Model-Development/scripts/mlflow_smoke.py  # wherever MLFLOW_TRACKING_URI points;
                                                      # unset: the local store (tracking.py)

The run goes to the smoke-test experiment, apart from real training. Exit 0 when all three
read back, 1 otherwise, including a server that does not answer (tracking.check_server).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import tracking

EXPERIMENT = "smoke-test"
PARAM = ("smoke_param", "1")
METRIC = ("smoke_metric", 0.5)
ARTIFACT = "smoke.txt"
UI_ENV = "BRT_MLFLOW_UI_URL"  # where the host's browser finds the server (set by the mlflow-smoke service)


def smoke(local_dir: Path | str | None = None) -> dict:
    """Log the smoke run, then read it back from the store; returns what came back."""
    uri = tracking.setup(EXPERIMENT, local_dir)  # a server must answer first

    import mlflow
    from mlflow.tracking import MlflowClient

    with tempfile.TemporaryDirectory() as tmp:
        note = Path(tmp) / ARTIFACT
        note.write_text(f"MLflow smoke check (#104), {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n",
                        encoding="utf-8")
        with mlflow.start_run(run_name="smoke") as run:
            mlflow.log_param(*PARAM)
            mlflow.log_metric(*METRIC)
            mlflow.log_artifact(str(note))
    client = MlflowClient(uri)
    back = client.get_run(run.info.run_id)
    return {
        "uri": uri,
        "experiment_id": run.info.experiment_id,
        "run_id": run.info.run_id,
        "param": back.data.params.get(PARAM[0]),
        "metric": back.data.metrics.get(METRIC[0]),
        "artifacts": [item.path for item in client.list_artifacts(run.info.run_id)],
    }


def problems(result: dict) -> list[str]:
    """What did not read back as logged; empty when the run is complete."""
    found = []
    if result["param"] != PARAM[1]:
        found.append(f"parameter {PARAM[0]} is {result['param']!r}, logged {PARAM[1]!r}")
    if result["metric"] != METRIC[1]:
        found.append(f"metric {METRIC[0]} is {result['metric']!r}, logged {METRIC[1]!r}")
    if ARTIFACT not in result["artifacts"]:
        found.append(f"artifact {ARTIFACT} is not among {result['artifacts']}")
    return found


def where_to_look(result: dict) -> str:
    """The run's page in the UI, or for a local store the command that opens one."""
    uri = result["uri"]
    if uri.startswith(("http://", "https://")):
        base = os.environ.get(UI_ENV, "").strip() or uri
        return f"{base.rstrip('/')}/#/experiments/{result['experiment_id']}/runs/{result['run_id']}"
    return f"local store {uri}; to browse it: mlflow ui --backend-store-uri {uri} --port 5001"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-dir", help="local store when MLFLOW_TRACKING_URI is unset (default Model-Development/mlruns)")
    args = parser.parse_args(argv)
    try:
        result = smoke(args.local_dir)
    except ConnectionError as exc:
        print(f"mlflow smoke FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"tracking URI {result['uri']}; experiment {EXPERIMENT} (id {result['experiment_id']}), run {result['run_id']}")
    print(f"read back: {PARAM[0]}={result['param']}, {METRIC[0]}={result['metric']}, artifacts {result['artifacts']}")
    missing = problems(result)
    if missing:
        print("mlflow smoke FAILED: " + "; ".join(missing), file=sys.stderr)
        return 1
    print(f"mlflow smoke OK. The run in the UI: {where_to_look(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
