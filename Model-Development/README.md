# Model Development

The models for Boston Resilience Twin, and the tools they train and log with. Each component has a section here; the operating steps are in [docs/runbook.md](../docs/runbook.md).

1. [MLflow tracking server (M1)](#mlflow-tracking-server-m1)

## MLflow tracking server (M1)

An MLflow server in Docker Compose (#104) that keeps every training run in one place: its parameters, metrics, tags and files, with a web UI to compare runs. MLflow tracks runs only; the one model registry is Vertex AI (ADR-006). Every command here was run on main on 2026-11-13 (macOS, Docker Desktop, Compose v5.0.1) with `MLFLOW_HOST_PORT=5050`.

**What it does**

- **One place for runs.** `train.py` and `selection.py` (#107) and the model workflow's steps (#112) log through `scripts/tracking.py`. When `MLFLOW_TRACKING_URI` names the server, their runs go there.
- **Nothing needs it.** With `MLFLOW_TRACKING_URI` unset or empty, runs go to a local store on disk instead, so a fresh clone, a GitHub Actions runner or the training image trains without the server. When the variable names a server that does not answer, a run stops within seconds and says how to start the server.
- **Off by default.** It starts only when asked for (compose profiles `mlflow` and `smoke`), so a plain `docker compose up`, which airflow-vm runs, never starts it (ADR-031).
- **Local only.** The UI binds `127.0.0.1` (ADR-016), on port 5000 or the port `MLFLOW_HOST_PORT` in `.env` names. macOS AirPlay Receiver often holds 5000; pick another, for example 5050.
- **Small.** One worker process and MLflow's job workers off: about 355 MiB of its own memory, under a 512 MiB limit.

**How to run it** (from the repo root; with `MLFLOW_HOST_PORT` set, use that port in place of 5000):

```bash
docker compose up -d --wait mlflow                  # start the server; UI at http://127.0.0.1:5000
docker compose run --rm --build mlflow-smoke        # log one test run from a container; starts mlflow if needed
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000    # host runs (train.py, selection.py) now log to the server
python Model-Development/scripts/mlflow_smoke.py    # the same test run, from the host
docker compose --profile mlflow down                # stop it; a plain `docker compose down` leaves it running
```

The smoke check prints `mlflow smoke OK` and the run's link in the UI. Containers on the compose network reach the server at `http://mlflow:5000`. Unset `MLFLOW_TRACKING_URI` to log to the local store again.

**What it produces.** Runs, grouped in experiments: `smoke-test` (the smoke check) and `model-training` (every `train.py` run, and each `selection.py` run with its models nested in it). A run holds its parameters, metrics, tags (model, data version, git commit) and files; the runbook's "MLflow tracking (#107)" lists what a training run logs.

**Where results live** (all gitignored):

- **The server's store:** `Model-Development/mlflow/`, with `mlflow.db` (SQLite: experiments, runs, parameters and metrics) and `artifacts/` (the files). Stopping the server keeps both.
- **The local store:** `Model-Development/mlruns/`, the same two parts. The runbook's "MLflow tracking (#107)" shows how to browse it with `mlflow ui`.
- **Which store a run used:** its `run.json` in `Model-Development/runs/<run id>/`, under `mlflow.where`.

**Code**

- `docker-compose.yaml`: the `mlflow` service (`ghcr.io/mlflow/mlflow:v3.17.2`, the version of the client in `requirements.txt`) and `mlflow-smoke`.
- `scripts/tracking.py`: where runs go.
- `scripts/mlflow_smoke.py`: the smoke check.
- `tests/test_tracking.py` and `tests/test_mlflow_service.py` (the second keeps the image tag equal to the client pin).

The operating steps, the port, memory and stopping are in the runbook's "MLflow tracking server (#104)".
