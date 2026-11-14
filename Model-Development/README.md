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
docker compose --profile mlflow down                # stop it, on any Compose version
```

The smoke check prints `mlflow smoke OK` and the run's link in the UI. Containers on the compose network reach the server at `http://mlflow:5000`. Unset `MLFLOW_TRACKING_URI` to log to the local store again. A plain `docker compose down` removes mlflow on Compose v5.5.1 and later but leaves it running on v5.0.1; `--profile mlflow` stops it on both.

**What it produces.** Runs, grouped in experiments: `smoke-test` (the smoke check), `model-training` (every `train.py` run, and each `selection.py` run with its models nested in it) and `model-tuning` (each `tune.py` study and each `compare_tuned.py` comparison, #109). A run holds its parameters, metrics, tags (model, data version, git commit) and files; the runbook's "MLflow tracking (#107)" lists what a training run logs.

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

## Model CI/CD pipeline and training image

Every change to the model code or its data inputs trains the baseline and the candidates in a pinned Docker image, picks the winner after the bias check, applies the promotion gates and checks that the saved model reloads (#112, #123). The workflow is [`.github/workflows/model.yml`](../.github/workflows/model.yml); the image is built from [`Model-Development/Dockerfile`](Dockerfile). One check, `model-ci`, must pass before a pull request merges. The commands below were run on main (`2afe4bc`) on 2026-11-13 (Linux, Docker 29.9.3), in a fresh clone.

| When | What runs | How it ends |
|---|---|---|
| Pull request that changes `Model-Development/`, `model.yml`, `.github/scripts/in_image.sh`, `Data-Pipeline/dvc.lock` or `Data-Pipeline/config/feature_catalog.csv` | Pull the data as `ci-reader`, build the image, train, evaluate, gates, reload check, upload the artifacts | A failed gate only warns and Slack says "gate failed"; `model-ci` still passes. An error fails it. Nothing is pushed |
| Push to main with such a change (or a manual dispatch, which always runs) | The same, as `ci-writer`, plus: the image is pushed right after the build and the champion re-score (a stub) runs before the gates; after the gates come the registry push and the champion step (stubs) | A failed gate fails the run, so the reload check, the registry push and the champion step do not run; the image is already pushed and the re-score has run. Slack says "training complete" or why not |
| Any change outside those paths | `changes` only; `pipeline` is skipped | `model-ci` passes |

**Prerequisites.** Python 3.12 (ADR-013) with `uv`, Docker, DVC 3.67.1 with dvc-gs 3.1.0 (`requirements-pull.txt`, the versions `model.yml` installs) and `jq`. Reading CI also needs `gh` (2.46.0 here) logged in, and listing images needs `gcloud` with read access to project 2.

**Access.** The training data is in a private bucket (`gs://boston-resilience-twin-data/dvc`); the repo holds pointers only. `dvc pull` needs Google application default credentials of an account the bucket admits (the `gcloud auth application-default login` line below; the [Data-Pipeline README](../Data-Pipeline/README.md#dvc-and-reproducibility) has more on DVC access). With no usable credentials it prints `ERROR: failed to connect to gs ... Anonymous caller does not have storage.objects.get access ... 401` and exits 1. A reader without access can still build the image, validate the gates in it, and run the tests (572 passed on a fresh clone with no data); they cannot run the chain, which reads the labels and features. Rebuilding the data from public sources is in the Data-Pipeline README.

**How to run it locally** (from the repo root; the chain takes about 75 s after the build):

```bash
uv venv --python 3.12 --seed .venv && source .venv/bin/activate
pip install -r Model-Development/requirements-pull.txt
gcloud auth application-default login   # opens a browser, once per machine
dvc pull Data-Pipeline/data/processed/labels.parquet Data-Pipeline/data/processed/features.parquet
docker build -f Model-Development/Dockerfile -t brt-train:local .
(   # a subshell: a failed step stops the block and names itself, and your shell stays open
  set -eu
  export IMAGE=${IMAGE:-brt-train:local} ARTIFACTS_DIR=$(mktemp -d)
  echo "artifacts in $ARTIFACTS_DIR"
  run() { bash .github/scripts/in_image.sh python "$@" || { echo "FAILED: $1 (exit $?)" >&2; exit 1; }; }
  for s in pull_data train evaluate rescore_champion; do
    run Model-Development/scripts/steps/step_$s.py --artifacts artifacts
  done
  run Model-Development/scripts/gates.py --metrics artifacts/metrics.json --out artifacts/gate_report.json
  echo "gates exit 0"
  for s in reload_check registry_push set_champion; do
    run Model-Development/scripts/steps/step_$s.py --artifacts artifacts
  done
  jq '.decision, .f1_gate.reason, .fairness_gate.reason' "$ARTIFACTS_DIR/gate_report.json"
)
```

Only the two parquet files are pulled (a bare `dvc pull` is about 2.26 GB). `in_image.sh` mounts them, the feature catalog, `dvc.lock` and `Model-Development/config` read-only, and `$ARTIFACTS_DIR` as `artifacts`. A step that exits non-zero prints `FAILED: <the script> (exit <code>)` and stops the block, so the gates never run after a failed step; a gate that fails (exit 1) stops it the same way, as it does on main. Good output ends with `gates exit 0` and `"promote"`, margin `0.221623` and gap `0.043454` on data version `a719afeb`, the numbers of CI run 43489397101. `registry_push` warns that `GITHUB_SHA` and `image_digest.txt` are missing: only CI sets them. The tests:

```bash
pip install -r Data-Pipeline/requirements.txt
python -m pytest Model-Development/tests            # 572 passed in about 107 s
docker compose run --rm --build test                # the full suite with coverage: 1618 passed, 2 skipped, 8 min 3 s
docker build -f Model-Development/Dockerfile -t brt-train:local . && docker run --rm brt-train:local python Model-Development/scripts/gates.py --validate-config
```

The `docker compose` line is the full suite, in the Airflow image with Python 3.12 and the DAG tests, and the same command the `Lint and test (Python 3.12)` check runs (`ci.yml`, run 43539609847); it was run locally on 2026-11-14 with exit 0. The `pytest` line runs the Model-Development tests alone.

**What it produces** (in `$ARTIFACTS_DIR`; CI uploads the same folder):

| File | Written by | Meaning |
|---|---|---|
| `data_version.txt` | `step_pull_data` | sha256 of `Data-Pipeline/dvc.lock`, the data version every later step checks |
| `runs/ci-<model>/` | `step_train` | Per model (`seasonal`, `logreg`, `xgboost`, `lightgbm`): `predictions.parquet`, `metrics.json`, `validation_counts.json`, `run.json`, and `model.joblib` for the three candidates |
| `mlruns/` | `step_train`, `step_evaluate` | MLflow's local store (`MLFLOW_TRACKING_URI` stays empty); the compose server is not used |
| `metrics.json`, `evaluation.json` | `step_evaluate` | The gates' input (validation counts, per-group counts); the winner, the ranking and why it was chosen |
| `bias/` | `step_evaluate` | The winner's validation bias report: `bias_report.md`, `bias.json`, `bias_slices.csv`, `bias_recall.png` (ADR-033) |
| `gate_report.json` | `gates.py` | The decision (`promote`, `keep_champion`, `blocked`, `error`), both gates with their numbers, and the sha256 of the gate files |
| `reload_check.json` | `step_reload_check` | Rows compared and the largest probability difference after reloading the winner |
| `champion_rescore.json`, `registry.json`, `champion.json` | the three stubs | What the real steps will do; `registry.json` says `pushed: false` |
| `image_digest.txt` | the workflow | The registry digest on main; the local image ID on a pull request |

`reports/` in the folder stays empty: `selection.py` is not run here.

**Where results live:**

| Output | Location | How to find it | Kept |
|---|---|---|---|
| Artifacts above | GitHub artifact `model-artifacts` of the run | `gh run list --workflow model.yml --branch main --limit 5`, then `D=$(mktemp -d); gh run download <run id> -D "$D"` | 90 days (run 43539609767 expires 2027-02-12) |
| Image | `us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images/brt-train:<git sha>` | `gcloud artifacts docker images list us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images/brt-train --include-tags`. A run's image is the digest in its `image_digest.txt`: the tag points to the newest push of that commit, so a dispatch or rerun on the same commit moves it and leaves the earlier image untagged | Until deleted; no cleanup policy yet |
| Run log, job summary | The run's page on GitHub | `gh run view <run id>` | 90 days |
| Slack message | The channel behind the `SLACK_ALERT_WEBHOOK` secret | Failures and main runs only | Slack's |

**Exit codes.** Steps exit 0 (ok) or 2 (pipeline failed); only `gates.py` exits 1 (a gate failed, decision `blocked`) and also 2 (it cannot evaluate, decision `error`).

| Code | On a pull request | On main |
|---|---|---|
| 0 | Reload check; `model-ci` passes | Reload check, then the registry push and champion stubs |
| 1 | Warning in the log and job summary, Slack "gate failed", run stays green | The run fails, nothing after the gates runs, Slack "gate failed". The image was already pushed |
| 2 | The run and `model-ci` fail, Slack "pipeline failed" with the step | The same |

Fixing a failed gate never means editing `gates.csv`: ADR-026 froze it.

**Stubs** (#111 replaces each; PR #142 is open): `step_rescore_champion.py` (no champion, so the candidate is judged against the baseline alone), `step_registry_push.py` (writes `registry.json`, pushes nothing to Vertex AI Model Registry) and `step_set_champion.py` (prints "would set champion").

**Links.** [ADR-026](../docs/decisions/ADR-026-promotion-gates.md) (gates), [ADR-028](../docs/decisions/ADR-028-model-pipeline-workflow.md) (workflow shape), [ADR-032](../docs/decisions/ADR-032-model-pipeline-real-steps.md) (real steps, image, identities), [ADR-033](../docs/decisions/ADR-033-model-bias-check.md) (bias check); the operating steps are in the runbook's [Model CI/CD pipeline (#123)](../docs/runbook.md#model-cicd-pipeline-123).
