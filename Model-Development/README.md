# Model Development

The models for Boston Resilience Twin, and the tools they train and log with. Each component has a section here; the operating steps are in [docs/runbook.md](../docs/runbook.md).

1. [MLflow tracking server (M1)](#mlflow-tracking-server-m1)
2. [Model bias check (M7)](#model-bias-check-m7)

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

## Model bias check (M7)

The bias part of model selection (#110, ADR-033). It checks every candidate against ADR-026's fairness gate on validation, picks the winner after that check, and reports how the model does for the groups #32 found. Every command here was run on main at `2afe4bc` on 2026-11-14 (macOS arm64, Docker Desktop), on data `a719afeb`.

**What it does**

- **Gates on poverty.** The gate compares recall on validation surge days between the two ACS poverty groups of `config/poverty_groups.csv`. Groups with fewer than 5 surges are pooled, and the gap must be at most 0.15. All of it comes from `config/gates.csv` through `gates.evaluate()`, so the check and `gates.py` use one definition.
- **Picks the winner after bias.** In the model workflow, `step_evaluate.py` ranks the candidates by validation F1 and picks the first whose gate passes. `gates.py` still decides promotion.
- **Reports the other groups, not gated:** people of color, limited English, flood zone and each neighborhood, on validation and test, with Fairlearn's `MetricFrame`. It also shows the higher-poverty group in two parts, student areas and the rest (#126).
- **Checks ADR-026's reopen conditions,** and reports them for the gate's owner without blocking promotion.
- **States the residual reporting bias:** storm requests per 1,000 residents a year by poverty group, which the labels still carry.

**How to run it** (from the repo root, after the data pull; the runbook's "Model bias check (#110, ADR-033)" has the details):

```bash
docker build -f Model-Development/Dockerfile -t brt-train:local .
A=$(mktemp -d); export IMAGE=brt-train:local ARTIFACTS_DIR=$A
for s in pull_data train evaluate; do bash .github/scripts/in_image.sh python Model-Development/scripts/steps/step_$s.py --artifacts artifacts || break; done
bash .github/scripts/in_image.sh python Model-Development/scripts/bias.py --run Model-Development/runs/ci-lightgbm   # validation and test, for one run
python Data-Pipeline/scripts/model_groups.py    # host only: rewrite config/slice_groups.csv after a change to #32's groups
```

On this data version every candidate passes: LightGBM 0.043, XGBoost 0.046, logistic regression 0.015. LightGBM, the best validation F1 (0.3435), is selected, and `gates.py` gives `promote`. The neighborhood reopen condition holds for West End and Fenway on validation. ADR-026's owner decided not to reopen (ADR-026, note of 2026-11-12).

**What it produces**

- `bias.json` (the gate, every group's numbers, the reopen conditions and the reporting bias), `bias_slices.csv`, `bias_report.md` and `bias_recall.png` (validation recall by group, the gated groups dark, with a line at the best gated recall minus the limit).
- In the workflow: the winner's validation report in `artifacts/bias/`, and `evaluation.json` with each candidate's gap and how the winner was chosen.
- In MLflow: the same files in the winner's run under `bias/`, the tags `bias_gate`, `bias_reopen_neighborhoods` and `bias_reopen_two_parts`, and the metric `validation_worst_group_recall_gap`.

**Where results live**

- **Workflow runs:** the `model-artifacts` upload of each model workflow run (`bias/`, `evaluation.json`, `metrics.json`).
- **A `bias.py` run:** the run folder `Model-Development/runs/<run id>/` (gitignored), or `--out-dir`.
- **MLflow:** the `model-training` experiment, in the winner's run.
- **The per-group numbers on this data version** are in ADR-033's "Results".

**Code**

- `scripts/bias.py`: the check, the slicing, the report and the CLI.
- `scripts/steps/step_evaluate.py`: selection after bias in the workflow.
- `scripts/gates.py`: `gates.evaluate()` and `gates.poverty_groups()`, the one reader of `poverty_groups.csv`.
- `config/slice_groups.csv`: the reported groups, written by `Data-Pipeline/scripts/model_groups.py` from #32's code, since the image has neither geopandas nor the static layers.
- `tests/test_bias.py` and `tests/test_steps.py`. In Data-Pipeline, `tests/test_bias_slicing.py` has the drift test for `slice_groups.csv`, which skips without the static layers, as in CI.
