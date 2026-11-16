# Model Development

The models for Boston Resilience Twin, and the tools they train and log with. Each component has a section here; the operating steps are in [docs/runbook.md](../docs/runbook.md).

1. [MLflow tracking server (M1)](#mlflow-tracking-server-m1)
2. [Project 2 infrastructure: registry, image repository, CI identities (M2)](#project-2-infrastructure-registry-image-repository-ci-identities-m2)
3. [Training core: candidate models, tracking, selection, hold-out results (M4)](#training-core-candidate-models-tracking-selection-hold-out-results-m4)
4. [Hyperparameter tuning with Optuna (M6)](#hyperparameter-tuning-with-optuna-m6)
5. [Model bias check (M7)](#model-bias-check-m7)
6. [Model registry: push, champion and rollback (M8)](#model-registry-push-champion-and-rollback-m8)
7. [Sensitivity analysis: SHAP and hyperparameter importance (M10)](#sensitivity-analysis-shap-and-hyperparameter-importance-m10)

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

## Project 2 infrastructure: registry, image repository, CI identities (M2)

The Google Cloud resources the model work runs on, in project 2 (`boston-resilience-twin-p2`, ADR-009), all from Terraform in [`Deployment/terraform-project2/`](../Deployment/terraform-project2/) (#105, ADR-030). Project 1 keeps the VM, the data and the DVC remote; project 2 holds what models and images need: a Docker repository for the training image (#112), a bucket for model files and the Vertex AI Model Registry they are registered in (#111, ADR-006), and the two keyless identities GitHub Actions uses. Every command here was run on main (`b9d69ce`) on 2026-11-14 (macOS, Terraform 1.16.4, google provider 8.10.0 from `.terraform.lock.hcl`); the plan again on `c87bffb` on 2026-11-15, after the Vertex AI service agent's grant (#153).

**What it does**

- **Keyless CI.** GitHub Actions trades its OIDC token for a short-lived token of a service account through Workload Identity Federation. No service account key exists (both accounts list 0 user-managed keys), and the provider accepts tokens from this repository only, matched on its numeric ids, which a renamed or recreated repository cannot reuse.
- **Two identities.** `ci-reader` for any run of the repository, pull requests included: no project 2 roles, only read access to project 1's DVC data, which project 1's Terraform grants (`Deployment/terraform/iam.tf`). `ci-writer` for runs on `refs/heads/main` only: it pushes the training image and registers models, with the narrowest roles that allow it (table below). A fork pull request gets no OIDC token, so it can use neither.
- **Least privilege for the registry.** No predefined Vertex AI role is limited to registering models (`roles/aiplatform.user` also deletes models, trains, deploys endpoints and calls Gemini), so a custom role, `modelRegistrar`, holds only `aiplatform.models.upload`, `.get`, `.list`, `.update` (aliases) and `aiplatform.operations.list`.
- **CI cannot overwrite model files.** `ci-writer` holds `storage.objectCreator` and `storage.objectViewer` on the model bucket and nothing that deletes, so a workflow run cannot overwrite or delete a registered version's files, which a rollback needs (ADR-006). People can: the project's Owner and the Editors in `project_admins` may overwrite or delete objects. The backstop is versioning: a replaced or deleted object stays as a noncurrent copy (no lifecycle rule removes them), restorable with `gcloud storage cp` from its generation. Public access prevention is enforced.
- **The Vertex AI service agent reads the image repository.** `models:upload` checks that project 2's Vertex AI service agent can read the version's container image, and its default role lacks a permission that check needs, so it has `artifactregistry.reader` on `brt-images` only (#153, ADR-030 note of 2026-11-15). Without it the first push on main failed with a 400.
- **A budget alert.** $50 (one member's credit), emailed to the billing account's Billing Account Administrators and Billing Account Users (the default recipients: the budget has no notification channel, so teammates in `project_admins` get none) at 25, 50 and 80 percent of spend before credits and at 80 percent of the month's forecast. Education credits bring net spend to about 0, so an alert on net spend would never fire.

**How to run it** (from `Deployment/terraform-project2/`, after `gcloud auth application-default login`; `terraform.tfvars` is copied from `terraform.tfvars.example`, holds the billing account and the teammates in `project_admins`, and is gitignored):

```bash
cd Deployment/terraform-project2
terraform init                                   # the GCS state backend and the google provider
terraform fmt -check && terraform validate       # no cloud calls; prints "Success! The configuration is valid."
terraform plan -var-file=terraform.tfvars        # read-only; on main (c87bffb): "No changes. Your infrastructure matches the configuration."
terraform output                                 # the six values below
```

`terraform apply` is human only and is never run by Claude Code; the runbook's "Project 2 infrastructure (Terraform)" has the apply, the costs and the teardown. The project and its state bucket were created by hand once (ADR-030 "Bootstrap"), because Terraform cannot create the bucket its own state lives in.

**What it produces**

| Resource | Name | Notes |
|---|---|---|
| APIs | Vertex AI, Artifact Registry, Billing Budgets, IAM, IAM Credentials, STS, Storage, Resource Manager, Service Usage | Cloud Run and Gemini are enabled with their services in Deployment |
| Docker repository | `us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images` | The training image `brt-train`, tagged with the commit; every main run pushes one (#112). 17 images, 4,368 MB on 2026-11-16 (16 and 3,604 MB on 2026-11-15); no cleanup policy yet, so it grows with every main run |
| Model bucket | `gs://boston-resilience-twin-p2-models` | us-east1, versioned, uniform access, public access prevention enforced, `force_destroy` off; one folder per training run, `surge-forecast/<run id>/<MLflow run id>/` (#111). On 2026-11-16: six folders, one per version 1 to 5 and one with no version, left by the push that failed before #153 |
| Model registry | Vertex AI Model Registry, us-east1 | Nothing to create: the model `surge-forecast`, its versions and aliases are data that #111 writes through the API. No endpoint (endpoints bill by the hour). On 2026-11-16: versions 1 to 5, version 1 `production`, version 5 `candidate` |
| Workload Identity | pool `github`, provider `github-oidc` | `ACTIVE`, condition `assertion.repository_id == '1404951557' && assertion.repository_owner_id == '71484962'` |
| Service accounts | `ci-reader`, `ci-writer` | Bound on `attribute.repository_id/1404951557` and `attribute.ref/refs/heads/main` |
| Custom role | `modelRegistrar` | The five permissions above |
| Budget | `boston-resilience-twin-p2 credit`, $50 | Four alert rules (above) |
| Vertex AI service agent grant | `artifactregistry.reader` on `brt-images` for `service-570429679915@gcp-sa-aiplatform.iam.gserviceaccount.com` | The image check at upload (#153) |
| Teammate access | `roles/editor` and `roles/resourcemanager.projectIamAdmin` | For the members in `project_admins` (Prit), in place of `roles/owner`, which cannot be granted through the API to an account outside the project's organization |

Who can do what in project 2:

| Identity | Used by | Roles |
|---|---|---|
| `ci-reader@boston-resilience-twin-p2.iam.gserviceaccount.com` | Any run of the repository (`model.yml` on pull requests and branches) | None in project 2; `storage.objectViewer` on project 1's `boston-resilience-twin-data` |
| `ci-writer@boston-resilience-twin-p2.iam.gserviceaccount.com` | Runs on main only (`model.yml` on main) | `modelRegistrar`; `artifactregistry.writer` on `brt-images` only; `storage.objectCreator` and `storage.objectViewer` on the model bucket; the same project 1 read |
| `service-570429679915@gcp-sa-aiplatform.iam.gserviceaccount.com` (Vertex AI service agent, created by Google) | Vertex AI itself, at `models:upload` | Its default role; `artifactregistry.reader` on `brt-images` only |

**Where results live.** `terraform output` on main printed:

```
ci_reader_email = "ci-reader@boston-resilience-twin-p2.iam.gserviceaccount.com"
ci_writer_email = "ci-writer@boston-resilience-twin-p2.iam.gserviceaccount.com"
image_repository = "us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images"
model_bucket = "boston-resilience-twin-p2-models"
project_number = "570429679915"
workload_identity_provider = "projects/570429679915/locations/global/workloadIdentityPools/github/providers/github-oidc"
```

These are public identifiers, not secrets; `.github/workflows/model.yml` writes the provider and both account emails in its auth steps. The state is in `gs://boston-resilience-twin-p2-tfstate` (prefix `project2`), outside Terraform. The billing account id is only in `terraform.tfvars` and the state, never in the repository.

**Code**

- `Deployment/terraform-project2/`: `apis.tf`, `artifact_registry.tf`, `storage.tf`, `ci_identity.tf` (the pool, the provider and both accounts), `iam.tf` (`modelRegistrar` and every grant), `budget.tf`, `outputs.tf`, `variables.tf`, `versions.tf` (the backend), `terraform.tfvars.example`.
- `Deployment/terraform/iam.tf`: project 1's read grant for both CI accounts on the data bucket (Prit, #112).
- [ADR-030](../docs/decisions/ADR-030-project-2-infrastructure.md) records the choices and the rejected options; [ADR-009](../docs/decisions/ADR-009-two-project-hosting.md) the two-project split.

The operating steps (cost, apply, checking the identities, a failed auth step, the budget, teardown) are in the runbook's "Project 2 infrastructure (Terraform)".

## Training core: candidate models, tracking, selection, hold-out results (M4)

The surge forecast itself (#107, on the training interface of #106). It trains a seasonal baseline and three candidate models, picks the best candidate on 2024, and scores it once on the hold-out window. Every command here was run on main on 2026-11-14 (Windows 11, Python 3.12.15, Docker Desktop), and the tables are the locked selection's, from the training image.

**What it does**

- **The question.** For each of the 24 neighborhoods (ADR-004) and each day: will the next day bring a surge of storm-related 311 requests? The labels are the frozen rule `sd_k2_m5` on the snow and ice and the tree groups (ADR-005, ADR-027), in the Data Pipeline's `labels.parquet`. The inputs are the columns of `features.parquet` that `Data-Pipeline/config/feature_catalog.csv` marks for the model, each known by 18:00 the evening before.
- **Time splits, never shuffled** (ADR-003): train 2015 to 2023, validation 2024, test 2025-01-01 to 2026-06-30. Rows from 2020 and 2021 weigh 0.5 in training.
- **Four models** (`scripts/models.py`):
  - the seasonal baseline: the share of surge days within two weeks of the same date in the 3 years before (ADR-005);
  - logistic regression;
  - XGBoost;
  - LightGBM.
- **Each candidate tunes itself on the train years only.** It uses three inner folds, 2019, 2022 and 2023, each fit on every train day before it. On them logistic regression picks its C, and XGBoost and LightGBM pick their number of trees. 2024 then picks only each model's alarm threshold (the one with the highest F1) and the winner. The test window is scored once, at the end.
- **The winner** (`scripts/selection.py`, ADR-026) is the candidate with the highest validation F1, if that F1 is at least the baseline's plus 0.05. Otherwise nothing wins and the command exits 1.
- **Days with no forecast are not scored.** Validation and test take their weather from the forecast issued the day before (ADR-003). 2024-01-01 to 2024-01-20 have none, so those 480 rows get no prediction and no say in the threshold. They stay in the data as the baseline's history.
- **Tracked and versioned.** Every model run is an MLflow run in the experiment `model-training`. The selection is the DVC stage `select` (`Model-Development/dvc.yaml`), whose `dvc.lock` ties the reports to the data version and the code.

**How to run it** (from the repo root, in the Python 3.12 venv of the Data Pipeline README's [Environment setup](../Data-Pipeline/README.md#environment-setup)).

- **LightGBM needs the OpenMP runtime.** The Airflow and training images have it. A Linux host needs `libgomp1`. On macOS, LightGBM's install notes say `brew install libomp` (not tried here).
- **Training reads two data files.** With access to the DVC bucket, pull just those (about 5 MB). Without it, build the data with the Data Pipeline README's full build: a one-year slice holds no train years.

```bash
dvc pull Data-Pipeline/data/processed/labels.parquet Data-Pipeline/data/processed/features.parquet
python Model-Development/scripts/train.py --model lightgbm         # one model: trains, scores both windows, prints a table; about 8 s
python Model-Development/scripts/selection.py --reports-dir Model-Development/runs/selection   # the four models and the decision; about 30 s
python Model-Development/scripts/evaluate.py --run Model-Development/runs/<run id>   # recompute a run's metrics and check its prediction file
python Model-Development/scripts/model_params.py                  # the parameters each candidate trains with
cd Model-Development && dvc status select                         # is the locked selection current for this checkout?
```

- **`train.py`.** `--model` takes `seasonal`, `logreg`, `xgboost` or `lightgbm`. Each run takes 8 to 20 s on a laptop. It exits 0, or 2 with `error:` and the reason.
- **`selection.py`.** It exits 0 with a winner, 1 when no candidate reaches the baseline plus 0.05, and 2 on an error. Keep `--reports-dir` outside `Model-Development/reports/`: without it, `selection.py` rewrites the DVC stage's own output there (see "Keep both steps' files out of `Model-Development/reports/`" under M10 below).
- **`dvc status select`** needs a checkout with LF line endings. `dvc.lock` hashes the scripts with LF endings, so a Windows checkout with `core.autocrlf` shows the stage as changed.
- **Locking a new selection.** After a new data version or promoted tuning, follow the runbook's "Run the training stage in the training image", so the lock holds CI's numbers.

**Tracking**

- **Where training runs:** on a laptop or in CI's training image (the model workflow, ADR-032), not on airflow-vm. No DAG trains a model there, no Airflow service mounts `Model-Development/`, and mlflow is not started there (ADR-031).
- **By default, no server.** With `MLFLOW_TRACKING_URI` unset or empty, runs go to the local store `Model-Development/mlruns/`. Browse it with `mlflow ui --backend-store-uri sqlite:///Model-Development/mlruns/mlflow.db` (http://localhost:5000).
- **The server.** Start it with `docker compose up -d --wait mlflow` ([MLflow tracking server (M1)](#mlflow-tracking-server-m1)), then run `export MLFLOW_TRACKING_URI=http://127.0.0.1:5000` before training. If the variable is set and the server is down, a run stops within seconds with "Start the server with `docker compose up -d --wait mlflow`, or unset MLFLOW_TRACKING_URI to log to the local store".

**What it produces**

- **A run folder per model run**, `Model-Development/runs/<run id>/`:
  - `predictions.parquet`: every scored row's probability and alarm, the contract in `scripts/predictions.py` that M6, M7, M9 and M10 build on;
  - `metrics.json`: per split, F1, precision and recall at the threshold, and AUC, PR-AUC, log loss and Brier skill, which do not depend on it;
  - `validation_counts.json`: validation tp, fp and fn with the data version, the gates' input (ADR-026);
  - `run.json`: the threshold, data version, git commit, settings, row counts, what the model chose on the inner folds, and where MLflow logged it;
  - `model.joblib`, for a candidate.
- **A selection's reports**, in `--reports-dir`:
  - `selection.json`: the decision, the rule and every model's numbers;
  - `comparison.csv` and `comparison.png`: validation F1 against the gate, with training time, scoring time for one evening's 24 rows and model size;
  - `confusion_matrix.png`: the winner's validation counts;
  - `test_slices.csv`: the winner against the baseline on the test window, overall and on storm, freeze and heat days.
- **MLflow runs** in `model-training`. Each model gets one run with its parameters, metrics, tags (model, data version, git commit) and files. A selection gets one parent run with its models nested in it.

**Where results live**

- **Run folders:** `Model-Development/runs/`, gitignored, only on the machine that ran them.
- **The locked selection:** `Model-Development/reports/`, the DVC stage's output, named by `Model-Development/dvc.lock`. `dvc pull Model-Development/reports` brings back its five files (about 140 KB). It comes from the training image, so the run folders it names are not on your machine.
- **MLflow:** the local store `Model-Development/mlruns/` or the server's store `Model-Development/mlflow/`, both gitignored. A run's `run.json` says which under `mlflow.where`.

**Hold-out results** (the locked selection `20261113T234855Z-selection`, from the training image; data version `a719afeb`)

Validation, 2024 (8,304 scored rows, 379 surges; each model at its own threshold):

| Model | Role | F1 | Precision | Recall | PR-AUC |
|---|---|---|---|---|---|
| LightGBM | selected | 0.3435 | 0.3100 | 0.3852 | 0.3368 |
| XGBoost | candidate | 0.3317 | 0.3124 | 0.3536 | 0.3214 |
| Logistic regression | candidate | 0.2888 | 0.4571 | 0.2111 | 0.2888 |
| Seasonal | baseline | 0.1219 | 0.0691 | 0.5198 | 0.0625 |

The gate is 0.1719, the baseline's F1 plus 0.05, and all three candidates reach it. A random ranking would have a PR-AUC of about 0.046, the surge rate.

Test, 2025-01-01 to 2026-06-30 (13,104 scored rows, 669 surges). LightGBM against the baseline, each at its validation threshold, with a 95% bootstrap interval over whole days for the F1 gain:

| Days | Count | Surges | LightGBM F1 | Baseline F1 | F1 gain (95% interval) |
|---|---|---|---|---|---|
| All | 546 | 669 | 0.4534 | 0.1549 | +0.2985 (+0.2257 to +0.3659) |
| Storm | 72 | 199 | 0.4790 | 0.2596 | +0.2194 (+0.0847 to +0.3315) |
| Freeze | 157 | 560 | 0.4954 | 0.2903 | +0.2051 (+0.1445 to +0.2597) |
| Heat | 9 | 3 | 0.0000 | 0.0385 | -0.0385 (-0.0870 to 0.0000) |

- **On the whole test window** LightGBM catches 71% of the surges (recall 0.7085), and one alarm in three is right (precision 0.3333). The baseline catches 76%, but fewer than one alarm in eleven is right (0.0862).
- **The kinds of day** are what the model knew by 18:00 the evening before (`config/slices.csv`):
  - storm: an alert in effect, an inch of forecast snow or rain, or a forecast gust of 46 mph;
  - freeze: a forecast low at or below 32 F;
  - heat: a forecast high of 90 F or more. Heat has only 3 surges in 9 days, so its numbers carry little weight.
- **Test F1 is higher than validation F1** partly because the test window has more surges: 5.1% of its rows, against 4.6% in 2024.
- **The other candidates on test.** The candidates are compared on validation only (ADR-003), but the locked `selection.json` holds every candidate's test numbers too:
  - logistic regression has the highest test F1: 0.5386, against 0.4534 for LightGBM and 0.4391 for XGBoost;
  - LightGBM and XGBoost keep the higher test PR-AUC, which does not depend on the threshold: 0.6072 and 0.5986, against 0.5654.

  So the gap is mostly in how well each model's 2024 threshold carries over to the test window.
- **Tuning (M6, #109)** found nothing better than these starting parameters, so `tuned_params.yaml` sets none.
- **Platforms.**
  - LightGBM, logistic regression and the baseline give the same numbers on Windows and in the images.
  - XGBoost does not. Its Windows build sums some values in a different order and grows a different model: 105 trees against 89 in the images, with its own threshold and confusion counts.
  - Its validation F1 happens to land close (0.3318 against 0.3317), but its test F1 does not (0.4793 against 0.4391).
  - These tables quote the images.

**Code**

- **Training and selection:**
  - `scripts/train.py`: trains one model by name and writes its run (#106; the interface ADR-027 fixes);
  - `scripts/selection.py`: the selection;
  - `scripts/slices.py`: the test results by kind of day;
  - `scripts/threshold.py`: the shared threshold rule and the surge metrics.
- **The models:**
  - `scripts/models.py`: the four models behind one interface;
  - `scripts/seasonal_baseline.py`: the baseline;
  - `scripts/inner_folds.py`: the inner folds;
  - `scripts/model_params.py`: each candidate's parameters (`config/model_params.yaml`, with M6's `config/tuned_params.yaml` over it).
- **Data and runs:**
  - `scripts/training_data.py`: the labels joined to the inputs, the splits and the unscored days;
  - `scripts/predictions.py`: the prediction file's contract;
  - `scripts/evaluate.py`: a run's check;
  - `scripts/mlflow_runs.py`: what a run logs to MLflow, through M1's `scripts/tracking.py`.
- **Settings:** `config/training.csv` (splits, weights, inner folds, forecast inputs, paths, candidates), `config/model_params.yaml` and `config/slices.csv`. The baseline and the 0.05 margin come from `config/gates.csv` (ADR-026).
- **The training stage:** `Model-Development/dvc.yaml` and `dvc.lock`.
- **Tests,** all in `tests/`, with `tests/training_helpers.py`: `test_train.py`, `test_selection.py`, `test_models.py`, `test_training_data.py`, `test_inner_folds.py`, `test_slices.py`, `test_threshold.py`, `test_seasonal_baseline.py`, `test_predictions.py`, `test_model_params.py`, `test_mlflow_runs.py`, and `test_dvc_stage.py`, which checks the stage's dependency list.

The operating steps are in the runbook's [Training interface (#106, ADR-027)](../docs/runbook.md#training-interface-106-adr-027) and the three sections after it: Model selection, Training stage (DVC) and MLflow tracking.

## Hyperparameter tuning with Optuna (M6)

`tune.py` searches the settings of the three candidate models (logistic regression, XGBoost and LightGBM) with Optuna, on the train years only. `compare_tuned.py` then compares what it found with the starting values on validation 2024 (#109). Neither script changes the model that training builds. Tuned values become active only through a reviewed step, and on the current data version they did not beat the starting values. So every candidate, LightGBM (the selected model) included, trains with its starting values in `Model-Development/config/model_params.yaml` (see "Why the starting values stay active" below). Every command here was run on main on 2026-11-13 (macOS arm64, an Apple M4 with 10 cores). The full studies, `--activate` and the comparison ran with copies of `tuning.csv` and `training.csv` that wrote outside the repo, and reproduced the committed files: only the timings, the creation time and the git commit differed.

**What it does**

1. **One Optuna study per candidate.** Each study uses the TPE sampler with a fixed seed and a fixed number of trials, from `Model-Development/config/tuning.csv`. Trial 0 is the starting values, so every study compares tuning with them. For logistic regression, trial 0's `C` is the one its starting `C_grid` picks on the inner folds.
2. **Scored on the train years only.** A trial fits the model on the train rows (2015 to 2023, ADR-003) and scores it on the inner fold years 2019, 2022 and 2023 (`inner_fold_years` in `Model-Development/config/training.csv`). Each fold year is refit on the train days before it and scored at the threshold the shared rule picks on that year. The trial's value is the mean of these fold F1s, averaged over the trial seeds 0, 1 and 2: the seed alone moves one fit's fold F1 by a standard deviation of 0.0048 (LightGBM) and 0.0039 (XGBoost). Logistic regression has no seed and is fitted once. Validation 2024 and the test window never reach a trial, so 2024 still only picks the threshold and the winner.
3. **Confirmed on fresh seeds.** Trial 0 and the 5 best other trials are fitted again on seeds 10 to 14, which no trial used.
   - A setting is eligible only if its mean inner-fold log loss is at most 1.005 times trial 0's.
   - The eligible setting with the highest mean fold F1 is the candidate.
   - The candidate is an improvement only if its mean gain over trial 0, paired by seed, is more than 2 standard errors. Otherwise the starting values are kept.
   - Logistic regression has no seed, so it pairs the three fold years instead and fits nothing new. That is a weaker check.
4. **Compared on validation 2024.** `compare_tuned.py` runs M4's `selection.py`, unchanged, twice on the same data version: once with the starting values alone, and once with the improvements in `best_params.yaml` over them. For each tuned model it records both runs' 2024 metrics, the share of rows on which both make the same surge call, and a paired bootstrap over whole days (1,000 draws, seed 0: `bootstrap_resamples` and `bootstrap_seed` in `training.csv`) of the F1 and log loss gains, with 95% intervals. The test window is not compared.

The search space is `SPACES` in `Model-Development/scripts/tune.py`. Each range holds the starting value. Everything else keeps its starting value, including the number of trees (each fit's early stopping on the inner folds picks it) and `down_weight` in `training.csv`.

| Model | Setting | Range searched | Starting value |
|---|---|---|---|
| Logistic regression | `C` | 0.001 to 100, log scale | picked from `C_grid` |
| | `year_parts` | 3 to 24 | 12 |
| XGBoost | `learning_rate` | 0.01 to 0.15, log scale | 0.05 |
| | `max_depth` | 2 to 8 | 4 |
| | `min_child_weight` | 0.5 to 50, log scale | 5.0 |
| | `reg_lambda` | 0.01 to 20, log scale | 5.0 |
| | `subsample` | 0.65 to 1 | 0.8 |
| | `colsample_bytree` | 0.6 to 1 | 0.8 |
| LightGBM | `learning_rate` | 0.01 to 0.15, log scale | 0.05 |
| | `num_leaves` | 4 to 63, log scale | 15 |
| | `min_child_samples` | 20 to 200, log scale | 100 |
| | `reg_lambda` | 0.01 to 20, log scale | 5.0 |
| | `subsample` | 0.65 to 1 | 0.8 |
| | `colsample_bytree` | 0.6 to 1 | 0.8 |

**Configuration.** Every tuning setting is in `Model-Development/config/tuning.csv`:

| Setting | Value | What it sets |
|---|---|---|
| `models` | `logreg;xgboost;lightgbm` | the candidates to tune, one study each |
| `trials` | 80 | trials per study; trial 0 is the starting values and the first 10 are TPE's random start |
| `seed` | 0 | the seed of the TPE sampler and of the fANOVA importance |
| `trial_seeds` | `0;1;2` | the model seeds every XGBoost and LightGBM trial is fitted with |
| `confirm_seeds` | `10;11;12;13;14` | the fresh seeds of the confirmation; none may be a trial seed |
| `confirm_top` | 5 | how many best trials, besides trial 0, the confirmation fits again |
| `guard_ratio` | 1.005 | the log loss guard: a confirmed setting is eligible only at or below this times trial 0's inner log loss |
| `stable_se` | 2 | an improvement needs a mean paired gain of more than this many standard errors |
| `study_dir` | `Model-Development/tuning` | where both scripts write their files |
| `mlflow_experiment` | `model-tuning` | the MLflow experiment of the studies and of the comparison |

The training settings (splits, inner folds, threshold rule, bootstrap) come from `training.csv`, and the starting values from `model_params.yaml`. Keep any changed copy of `tuning.csv` outside `Model-Development/config`: the training stage depends on that folder, so a change there makes `dvc repro` rerun the selection.

**How to run it** (from the repo root, in the Python 3.12 venv of the Data Pipeline README's [Environment setup](../Data-Pipeline/README.md#environment-setup), whose requirements include `optuna==5.0.0`). Training's two data files must be in place: `dvc pull` fetches them (needs `gcloud auth application-default login`), or the Data Pipeline README builds them.

```bash
dvc pull Data-Pipeline/data/processed/labels.parquet Data-Pipeline/data/processed/features.parquet
python Model-Development/scripts/tune.py            # the three studies: about 45 minutes on a laptop, most of it LightGBM's
python Model-Development/scripts/compare_tuned.py   # the 2024 comparison: about 30 seconds
python Model-Development/scripts/model_params.py    # check the parameter files and print every candidate's values, tuned ones marked
```

| Option | Script | What it does |
|---|---|---|
| `--models 'logreg;lightgbm'` | `tune.py` | tune only these models (default: `models` in `tuning.csv`); quote the value, since `;` ends a shell command |
| `--trials N` | `tune.py` | trials per study instead of `trials`, for a timing or smoke run |
| `--activate` | `tune.py` | also copy `best_params.yaml` into `tuned_params.yaml`, the file training reads; off by default (see "Promote tuned values" below) |
| `--candidate FILE` | `compare_tuned.py` | the tuned values to compare (default: `best_params.yaml` in `study_dir`) |
| `--id NAME` | `compare_tuned.py` | the comparison's name, also the prefix of its run folders (default: the UTC time) |
| `--config FILE`, `--tuning FILE` | both | another `training.csv` or `tuning.csv` |

Both scripts exit 0 when they finish, and 2 on a bad setting, missing data or a failed check, printing `error:` and the reason.

**Every run rewrites the committed files in `Model-Development/tuning/`.** A narrowed run (`--models`, `--trials`) writes only its own models into `summary.json`, `importance.csv` and `best_params.yaml`, and `compare_tuned.py` writes a new `comparison.json`. To try the scripts and keep the committed files as they are, give them a copy of `tuning.csv` that writes to the gitignored `Model-Development/runs/`:

```bash
mkdir -p Model-Development/runs/tuning-check
sed -e 's#^study_dir,[^,]*#study_dir,Model-Development/runs/tuning-check#' \
    -e 's#^mlflow_experiment,[^,]*#mlflow_experiment,model-tuning-check#' \
    Model-Development/config/tuning.csv > Model-Development/runs/tuning-check/tuning.csv
python Model-Development/scripts/tune.py --tuning Model-Development/runs/tuning-check/tuning.csv --models lightgbm --trials 2
python Model-Development/scripts/compare_tuned.py --tuning Model-Development/runs/tuning-check/tuning.csv \
    --candidate Model-Development/tuning/best_params.yaml
```

The comparison names the committed `best_params.yaml` because a 2-trial study rarely confirms an improvement, and `compare_tuned.py` stops on a file that tunes no model. Its MLflow runs go to the experiment `model-tuning-check`, apart from the real studies.

**What it produces**

| File | Written by | What it holds |
|---|---|---|
| `Model-Development/tuning/<model>.json` | `tune.py` | one study (`logreg`, `xgboost`, `lightgbm`): the method, the data version, the git commit, the search space, every trial's settings and scores, and the confirmation. `tune.load_study` rebuilds the study from it, as M10 (#113) does. |
| `Model-Development/tuning/importance.csv` | `tune.py` | each setting's importance (fANOVA, seeded) for the inner-fold log loss and for the fold F1 |
| `Model-Development/tuning/summary.json` | `tune.py` | for each model: trial 0, the best trial, the confirmed candidate with its gain and standard error, and the decision |
| `Model-Development/tuning/best_params.yaml` | `tune.py` | the improvements' values that differ from the starting ones, in the `tuned_params.yaml` format; not active, since no training run reads it |
| `Model-Development/tuning/comparison.json` | `compare_tuned.py` | both selections' 2024 metrics for every model and, for a tuned model, the same-call share and the paired bootstrap |
| `Model-Development/config/tuned_params.yaml` | `tune.py --activate` only | the tuned values training reads; today it holds comments only, so it sets nothing |
| `Model-Development/runs/` (gitignored) | `compare_tuned.py` | the run folder of every model both selections train, and each selection's reports in `<id>-default-reports/` and `<id>-tuned-reports/` |

MLflow runs, in the experiment `model-tuning`:

- `tune-<model>`: one run per study, tagged with the data version, the git commit and the method. It logs trial 0's and the candidate's scores, the importances, the decision (a tag) and the study file.
- `tune-<model>-trial-<n>`: a child run for each trial, with its settings and scores (80 per study).
- `tune-<model>-confirm-trial-<n>`: a child run for each confirmed setting, with its gain, its standard error, and whether it is eligible and an improvement (6 per study).
- `tune-summary`: `summary.json`, `importance.csv` and `best_params.yaml`.
- `<UTC time>-tune-compare`: one run per comparison, with both selections nested in it. It logs the F1 and log loss gains with their intervals, the same-call shares and `comparison.json`.

**Where results live**

- **In git:** `Model-Development/tuning/` (the studies and the comparison, small text files) and `Model-Development/config/tuned_params.yaml`.
- **Gitignored:** `Model-Development/runs/`, the comparison's run folders and reports. Neither script writes `Model-Development/reports/`, the training stage's output.
- **MLflow:** the server when `MLFLOW_TRACKING_URI` names it, else the local store `Model-Development/mlruns/`, as for training ([MLflow tracking server (M1)](#mlflow-tracking-server-m1)). To browse the local store, run `mlflow ui --backend-store-uri sqlite:///Model-Development/mlruns/mlflow.db` and open http://localhost:5000 (add `--port 5001` when 5000 is taken).

**Reproducibility**

- **Seeds.** The sampler, the trial seeds, the confirmation seeds and the fANOVA importance all take their seeds from `tuning.csv`. A rerun on the same data version and platform gives the same trials and the same `best_params.yaml`. Only the timings (`seconds`), the creation time and the git commit change.
- **Data version.** Every file and every MLflow run records the data version (the sha256 of `Data-Pipeline/dvc.lock`, with LF line endings) and the git commit. The studies belong to the data they ran on: on a new data version, run both scripts again.
- **Platforms.** The committed studies come from macOS (arm64). XGBoost's fits differ slightly between platforms, so its study repeats exactly only on the same platform (runbook, [Hyperparameter tuning (#109)](../docs/runbook.md#hyperparameter-tuning-109), "Platforms").

**Current results.** The committed files are for data version `a719afeb`: studies of 2026-11-12, with 80 trials per model on 70,008 train rows.

| Model | Trial 0 fold F1 | Candidate fold F1 | Gain (SE) | Decision |
|---|---|---|---|---|
| Logistic regression | 0.2345 | 0.2499 (trial 50) | +0.0154 (0.0097) | starting values kept |
| XGBoost | 0.2636 | 0.2676 (trial 62) | +0.0040 (0.0026) | starting values kept |
| LightGBM | 0.2635 | 0.2797 (trial 62) | +0.0162 (0.0017) | improvement, written to `best_params.yaml` |

The XGBoost and LightGBM rows are scored on the confirmation seeds 10 to 14, and the logistic regression row over the three fold years. The setting that mattered most for the fold F1 (`importance.csv`) was `year_parts` for logistic regression (0.98) and `max_depth` for XGBoost (0.42). For LightGBM it was `colsample_bytree` (0.27), then `learning_rate` (0.22).

LightGBM on validation 2024 (`comparison.json`: 346 scored days, 8,304 rows):

| Metric | Starting values | Tuned |
|---|---|---|
| F1 | 0.343529 | 0.343675 |
| Precision / recall | 0.3100 / 0.3852 | 0.3137 / 0.3799 |
| Log loss | 0.146544 | 0.147811 |
| PR-AUC | 0.3368 | 0.3248 |
| Brier skill | 0.1611 | 0.1518 |
| tp / fp / fn | 146 / 325 / 233 | 144 / 315 / 235 |
| Trees (picked by early stopping) | 88 | 265 |

The other models are not tuned, so both runs give them the same numbers. LightGBM wins both selections.

**Why the starting values stay active**

- **Nothing is activated.** `tuned_params.yaml` sets nothing, so every candidate trains with its starting values. The selected model is therefore the default LightGBM.
- **The gain did not carry over to 2024.** LightGBM's F1 gain is +0.000146. Its 95% interval, [-0.0161, +0.0148], holds zero, and the tuned version came out ahead in 49.6% of the draws. Its log loss is worse by 0.00127 (interval [+0.00005, +0.00274]). The two versions make the same call on 99.1% of the rows.
- **The gain fades in recent years.** On the train folds it is +0.0348 in 2019, +0.0188 in 2022 and -0.0050 in 2023, the fold closest to 2024.
- **Promotion is a reviewed step.** The tuned values stay in `best_params.yaml` as evidence.

**Promote tuned values**, only after a PR reviews a comparison that favors them:

1. `python Model-Development/scripts/tune.py --activate` runs the studies again and copies `best_params.yaml` into `tuned_params.yaml`.
2. `python Model-Development/scripts/model_params.py` checks the file and marks the tuned values.
3. Run the training stage in the training image, then commit `tuned_params.yaml` with `Model-Development/dvc.lock`.

**Code**

- `scripts/tune.py`: the studies, the confirmation, the search space (`SPACES`) and `load_study`.
- `scripts/compare_tuned.py`: the comparison on validation 2024.
- `scripts/model_params.py`: the check of `model_params.yaml` and `tuned_params.yaml`.
- `config/tuning.csv`, `config/model_params.yaml` (the starting values) and `config/tuned_params.yaml` (the tuned values training reads).
- `tests/test_tune.py` (the settings, the search spaces, the confirmation rule, a 2-trial run, that no validation or test row reaches a trial, and that the same seed gives the same studies) and `tests/test_compare_tuned.py` (the comparison), both in the full test suite.

The operating steps, with the exact commands of the promotion, are in the runbook's [Hyperparameter tuning (#109)](../docs/runbook.md#hyperparameter-tuning-109).

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
```

After a change to #32's groups or the static layers, rewrite `config/slice_groups.csv` on the host (the runbook's "Regenerate `slice_groups.csv`" has the drift test and the `select` stage refresh):

```bash
python Data-Pipeline/scripts/model_groups.py
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

## Model registry: push, champion and rollback (M8)

The last steps of the model workflow on main (#111, ADR-006). They register each winning model as a version in project 2's Vertex AI Model Registry, compare it with the current champion, and promote it only when ADR-026's gates say so. A person can roll the champion back with one command. Vertex AI is the only registry; MLflow tracks runs only. Every command here was run on main at `c87bffb` on 2026-11-15 (macOS arm64, Docker Desktop), on data `a719afeb`, against the live registry, except `rollback`, which moves the live champion.

**What it does**

- **One model, many versions.** Every push is a new version of `surge-forecast`. Three aliases say what each version is: `candidate` (the newest push), `production` (the champion) and `previous` (what a rollback restores). Vertex's own `default` alias is never used.
- **Re-scores the champion first.** `rescore` downloads the `production` version's files, checks `model.joblib` against the sha256 in its `metadata.json`, and scores it on this data version's validation rows with the shared threshold rule. The result is the `champion` block of the gates input, so `gates.py` compares both models on the same rows.
- **Pushes the winner.** `push` uploads the winner's files and a `metadata.json` (metrics, data version, git commit, MLflow run id, training image digest, the train, validation and test windows, the `gates.csv` sha256 and every file's sha256) to a new folder in the model bucket, registers the version with the alias `candidate`, and tags the MLflow run with the version. It pushes only on the gate decisions `promote` and `keep_champion`, and refuses a run trained on a tree with uncommitted changes.
- **Promotes by the rollback rule.** `promote` moves `production` to the pushed version, and `previous` to the old champion, only on `promote`. If moving `production` fails, it puts `previous` back.
- **Never overwrites.** The bucket folder is `<run id>/<MLflow run id>/`, new for every training run, and `ci-writer` can create objects but not delete them, so every version's files stay in place for a rollback.
- **Fails loudly on main, skips elsewhere.** Without project 2 access the steps log `skipped` and exit 0, so a pull request or a laptop run stays green. On main they pass `--require-access`: no access, or a 401 or 403 after the first call, exits 2 and Slack names the step.

**How to run it**

In the workflow, on every push to main (`.github/workflows/model.yml`): `step_rescore_champion.py`, then `gates.py`, `step_reload_check.py`, `step_registry_push.py` and `step_set_champion.py`. Only these three registry steps get `ci-writer`'s credentials (ADR-035).

Before them, the workflow builds the training image and checks it against `Model-Development/requirements.lock` (#155): every package of the image that version 1 was trained in, transitive ones included, which the Dockerfile installs with `-c`. The step "Check the image against the lock" diffs the built image's `pip freeze` against the lock, so the champion is always re-scored with the libraries it was trained with. On main the image is then pushed to `brt-images` (main only), and the push writes the registry digest to `artifacts/image_digest.txt`, which `step_registry_push.py` records as the version's container image. The runbook's "Training image package lock (#111)" has the check and how to change a package.

On a laptop, the local chain in CLAUDE.md runs the same steps in the training image. Without `MOUNT_GCP_CREDENTIALS=1` it has no credentials, so the registry steps skip and nothing is written to project 2. Run this way, LightGBM won with validation F1 0.3435 (seasonal baseline 0.1219), the reload check passed (8,304 validation rows, largest probability difference 0), and each registry step logged `skipped: no project 2 access`. Before its skip line the push step also prints `::warning::artifacts/image_digest.txt holds no registry digest of us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images/brt-train: None; the version names the image without a digest`: the local chain builds no registry image, so there is no digest (on main a missing digest is an error instead). With no champion block, `gates.py` gives `promote`.

From the repo root, after `gcloud auth application-default login` (the runbook's "Model registry (#111, ADR-006)" has every option and failure case). Two things to know when running them on the local chain's output (`$A` is its `ARTIFACTS_DIR`):

- `push` reads the winner's run folder from `training.csv`'s `runs_dir`, `Model-Development/runs/`, but the chain writes its runs to `$A/runs/`. Copy them first, `cp -R "$A/runs/." Model-Development/runs/` (gitignored), or the dry run stops with `cannot read the run record .../Model-Development/runs/ci-lightgbm/run.json` and exit 2. With the copy and `GITHUB_SHA` set, `push --gate-report "$A/gate_report.json" --evaluation "$A/evaluation.json" --dry-run` printed the plan and exited 0 (2026-11-16).
- `rescore` writes the champion into its `--metrics` file in place. Run it on a copy of `$A/metrics.json` if the chain's own gates input should stay as the chain left it.

```bash
python Model-Development/scripts/registry.py status                           # every version and its aliases
python Model-Development/scripts/registry.py rescore --metrics <gates input>  # the champion on this data version
python Model-Development/scripts/registry.py push --gate-report <gate report> --evaluation <evaluation.json> --dry-run   # the plan, no calls
python Model-Development/scripts/registry.py rollback [--to <version>]        # a person only
python Model-Development/scripts/lock_check.py                                # the code each dvc.lock recorded
```

Before the first push, `status` printed `no registry model named 'surge-forecast' yet` and `rescore` wrote `"champion": null` with the reason `no version holds production`; both exit 0. The dry run prints the version it would register (labels `model: lightgbm`, `gate_decision: promote`, `data_version: a719afeb...`, the git commit, and the folder `gs://boston-resilience-twin-p2-models/surge-forecast/ci-lightgbm/<MLflow run id>/`) and exits 0. `lock_check.py` prints `159 tracked dependencies match`.

The first two runs on main:

| Run | Champion before | Gates | Registry |
|---|---|---|---|
| 43749795990, attempt 2 (`b42316c`) | none | `promote`: F1 0.3435 against the seasonal baseline's 0.1219 (margin 0.22, at least 0.05), recall gap between the poverty groups 0.043 (at most 0.15) | version 1, then `production` |
| 43758289814 (`c87bffb`) | version 1, re-scored: tp 146, fp 325, fn 233 at threshold 0.0960 | `keep_champion`: both gates pass, but F1 0.3435 does not beat the champion's 0.3435 | version 2 `candidate`; version 1 stays `production` |

`status` with a laptop login, a snapshot after the second run (`c87bffb`, 2026-11-15):

```text
$ python Model-Development/scripts/registry.py status
version    2  candidate                    lightgbm  data a719afeb9695 commit c87bffbbf58c gates keep_champion 2026-11-15T20:31:49.826747Z
version    1  default,production           lightgbm  data a719afeb9695 commit b42316cc985a gates promote       2026-11-15T20:18:10.470870Z
```

Every later merge that touches `Model-Development/` retrains and pushes again, so `status` grows by a version each time: the merges of #151, #155 and #150 added versions 3, 4 and 5, each `keep_champion` against version 1 with the same re-scored numbers.

**Rollback, exercised live** (2026-11-16, a laptop login, on versions 1 to 5). Versions 1 and 2 hold the same `model.joblib` and nothing serves from the registry yet, so only aliases moved, and the second command put `production` back on version 1:

```text
$ python Model-Development/scripts/registry.py status
version    5  candidate                    lightgbm  data a719afeb9695 commit 19802e00d942 gates keep_champion 2026-11-16T01:10:11.182272Z
version    4  -                            lightgbm  data a719afeb9695 commit d7001731c288 gates keep_champion 2026-11-16T00:36:33.476433Z
version    3  -                            lightgbm  data a719afeb9695 commit 23ce40934d80 gates keep_champion 2026-11-16T00:22:49.226929Z
version    2  -                            lightgbm  data a719afeb9695 commit c87bffbbf58c gates keep_champion 2026-11-15T20:31:49.826747Z
version    1  default,production           lightgbm  data a719afeb9695 commit b42316cc985a gates promote       2026-11-15T20:18:10.470870Z
$ python Model-Development/scripts/registry.py rollback --to 2
rolled back: production moved from version 1 to 2
$ python Model-Development/scripts/registry.py status
version    5  candidate                    lightgbm  data a719afeb9695 commit 19802e00d942 gates keep_champion 2026-11-16T01:10:11.182272Z
version    4  -                            lightgbm  data a719afeb9695 commit d7001731c288 gates keep_champion 2026-11-16T00:36:33.476433Z
version    3  -                            lightgbm  data a719afeb9695 commit 23ce40934d80 gates keep_champion 2026-11-16T00:22:49.226929Z
version    2  production                   lightgbm  data a719afeb9695 commit c87bffbbf58c gates keep_champion 2026-11-15T20:31:49.826747Z
version    1  default                      lightgbm  data a719afeb9695 commit b42316cc985a gates promote       2026-11-15T20:18:10.470870Z
$ python Model-Development/scripts/registry.py rollback --to 1
rolled back: production moved from version 2 to 1
$ python Model-Development/scripts/registry.py status
version    5  candidate                    lightgbm  data a719afeb9695 commit 19802e00d942 gates keep_champion 2026-11-16T01:10:11.182272Z
version    4  -                            lightgbm  data a719afeb9695 commit d7001731c288 gates keep_champion 2026-11-16T00:36:33.476433Z
version    3  -                            lightgbm  data a719afeb9695 commit 23ce40934d80 gates keep_champion 2026-11-16T00:22:49.226929Z
version    2  -                            lightgbm  data a719afeb9695 commit c87bffbbf58c gates keep_champion 2026-11-15T20:31:49.826747Z
version    1  default,production           lightgbm  data a719afeb9695 commit b42316cc985a gates promote       2026-11-15T20:18:10.470870Z
```

Both rollbacks exited 0. `rollback --to` moves only `production`: no version held `previous` before, and none did after.

`rescore` on the laptop chain's gates input gives the same champion numbers as the main run (`champion version 1 on sha256:a719afeb9695: tp 146 fp 325 fn 233 (threshold 0.0960)`), and `gates.py` then gives `keep_champion`. Vertex puts `default` on the first version by itself; nothing reads it.

**What it produces**

- **In the registry:** a version of `surge-forecast` whose container image is the training image by digest, with labels for the model, git commit, MLflow run id, gate decision and the first 32 hex characters of the data version, `gates.csv` and gate report sha256 values. The description has the validation F1 and where the files are.
- **In the model bucket:** `gs://boston-resilience-twin-p2-models/surge-forecast/<run id>/<MLflow run id>/` with `model.joblib`, `metrics.json`, `run.json`, `validation_counts.json`, `evaluation.json`, the gate report and `metadata.json`.
- **In the workflow artifacts:** `champion_rescore.json`, `gate_report.json`, `push.json` (the push record `promote` reads, or `{"skipped": ...}` without access), `registry.json` (the version as `<model resource>@<version id>`, null when skipped, with the image digest and stamps) and `promotion.json`.
- **In MLflow:** the tags `registry_model` and `registry_version` on the winner's run, when MLflow answers.

**Where results live**

- **What serves now:** `registry.py status`, or the Vertex AI Model Registry in the console for project 2 (`boston-resilience-twin-p2`, us-east1).
- **A version's files and full record:** the bucket folder its description names, `metadata.json` first.
- **Each workflow run:** the `model-artifacts` upload of the model workflow run.

**Code**

- `scripts/registry.py`: `push`, `promote`, `rollback`, `status` and `rescore`.
- `scripts/vertex_client.py`: the Vertex AI and Cloud Storage REST calls through google-auth, with the quota project for a user login only.
- `scripts/lock_check.py`: the git-tracked dependencies of both `dvc.lock` files against the tree (CI runs it after lint).
- `scripts/steps/step_rescore_champion.py`, `step_registry_push.py` and `step_set_champion.py`: the workflow's calls.
- `registry/registry.csv`: project, region, model name, bucket, serving image and aliases. It is outside `config/` so a registry setting does not retrain the model.
- `tests/test_registry.py`, `tests/test_vertex_client.py`, `tests/test_lock_check.py` and `tests/test_steps.py`.

## Sensitivity analysis: SHAP and hyperparameter importance (M10)

`sensitivity.py` explains the selected model (#113) in two ways. Its SHAP values show which inputs move the model's surge forecasts on the test window, and the fANOVA importances of M6's studies (#109) show how much each tuning setting mattered. It only reads results and writes a report: it changes no model and no setting, and neither the training stage nor CI runs it. Run it by hand after a selection, on the machine or in the image run that made the selection. Every command in this section's code blocks, and the `docker build` before the image's, was run on main on 2026-11-14. The two Mac commands ran word for word in a copy of the checkout with its data files, in a new venv from `Data-Pipeline/requirements.txt` (macOS arm64, an Apple M4 with 10 cores). They only added gitignored files (`Model-Development/runs/`, `Model-Development/mlruns/` and Python's `__pycache__`), and every other file of the copy, `Model-Development/reports/` included, stayed byte for byte the same. The image block ran in the training image built from main. Both gave the SHAP results under "Current results" exactly. The DVC behavior in the warning below was checked in three throwaway DVC repos, each with a one-stage stand-in for the training stage: with a folder added to the output, `dvc status` and `dvc check-ignore` in one and `dvc status` and `dvc commit -f select` in another, and with an output file rewritten, `dvc status` and `dvc commit -f select` in the third. On the checkout, only the read-only `dvc status` and `dvc check-ignore` ran, so the training stage's output and lock stay as they are. `dvc pull`, which this section only mentions, was not run.

**What it does**

1. **Picks the model.** It explains the winner that `selection.json` names, or the best candidate when nothing won, marked as such. It loads that run's `model.joblib`, `run.json` and `predictions.parquet` from `Model-Development/runs/<run folder>/`.
2. **Checks that it explains the right thing.** It stops when `selection.json` comes from another data version than the checkout's, when the run folder has no `model.joblib`, or when the saved model no longer gives back its run's own test probabilities (to within 1e-9).
3. **Computes SHAP values on the test window** (2025-01-01 to 2026-06-30, ADR-003), in log-odds of the surge probability. LightGBM and XGBoost get shap's TreeExplainer, exact, on every scored test row, and a tree model's values plus the base value must give back each row's probability to within 1e-6. Any other model gets the permutation explainer on 500 test rows drawn with seed 0, against 100 train rows.
4. **Ranks the inputs and picks three example days.** For each input it reports the mean absolute SHAP value, its share of the total, its direction and its missing share. The direction is the rank correlation of the input's value with its SHAP value: 0.3 or more reads "higher value, higher forecast", -0.3 or less "higher value, lower forecast", anything between "mixed". The example days are the surest true alarm, the surest false alarm and the missed surge with the lowest probability.
5. **Measures the tuning settings.** It rebuilds each study in `Model-Development/tuning/` with `tune.load_study` and computes each setting's seeded fANOVA importance for the fold F1 and the inner log loss, as `tune.py` computes `importance.csv`. It compares the two: they match on the machine that ran the studies, and in the Linux training image they differ by up to 0.004, in the same order. A model without a study file is noted, not fatal.

**How to run it** (from the repo root, in the Python 3.12 venv of the Data Pipeline README's [Environment setup](../Data-Pipeline/README.md#environment-setup), whose requirements include `shap==0.54.1`). A venv set up before #113 needs `pip install -r Data-Pipeline/requirements.txt` again: without `shap`, the run stops at `import shap` with a traceback. It needs the two data files training reads, and a selection run on the same machine. `selection.py` writes `selection.json` and its other reports to `--reports-dir`, and the run folders it names to `Model-Development/runs/`; neither git nor DVC stores run folders. A `selection.json` from `dvc pull`, or one copied out of the training image, names run folders this machine does not have, and the run stops with "has no model.joblib; run selection.py again". So make the selection first and give its `selection.json` to `sensitivity.py`:

```bash
python Model-Development/scripts/selection.py --reports-dir Model-Development/runs/selection   # about 40 s: selection.json, its other reports and the run folders
python Model-Development/scripts/sensitivity.py --selection Model-Development/runs/selection/selection.json --out Model-Development/runs/sensitivity   # about 10 s
```

**Keep both steps' files out of `Model-Development/reports/`.** That folder is the output of the DVC training stage (`Model-Development/dvc.yaml`), and DVC does not ignore anything in it. Without `--reports-dir`, `selection.py` rewrites the stage's own five files there (`selection.json`, `comparison.csv`, `comparison.png`, `confusion_matrix.png`, `test_slices.csv`). Without `--out`, `sensitivity.py` writes to `Model-Development/reports/sensitivity/`. While such files are there, `dvc status` lists the stage's `reports` as modified, and `dvc commit -f select` would record them as the stage's output. `Model-Development/runs/` is gitignored and no stage's output.

| Option | What it does |
|---|---|
| `--out DIR` | where the files go (default `Model-Development/reports/sensitivity/`) |
| `--selection FILE` | the `selection.json` to explain (default: the one in `reports_dir`); its run folders must be in `runs_dir` |
| `--config FILE` | another `training.csv`: its `runs_dir` is where the run folders are read, its `reports_dir` gives the two defaults above, and its `mlflow_store` is where the MLflow run goes when `MLFLOW_TRACKING_URI` is unset |
| `--tuning FILE` | another `tuning.csv`, whose `models`, `seed` and `study_dir` pick the studies and the fANOVA seed (default: the committed studies in `Model-Development/tuning/`) |

It exits 0 when it finishes, and 2 when an input is wrong, printing `sensitivity:` and the reason.

**In the training image**, the selection and the analysis share one artifacts folder outside the checkout, so the defaults are safe there: `selection.py`'s reports and `sensitivity.py`'s files go to `$A/reports/`. After building the image (`docker build -f Model-Development/Dockerfile -t brt-train:local .`):

```bash
A=$(mktemp -d); export ARTIFACTS_DIR=$A IMAGE=brt-train:local
bash .github/scripts/in_image.sh python Model-Development/scripts/selection.py     # about a minute: the selection and its run folders, in $A
bash .github/scripts/in_image.sh python Model-Development/scripts/sensitivity.py   # about 6 s: $A/reports/sensitivity/ and one MLflow run in $A/mlruns/
```

**What it produces**

| File | What it holds |
|---|---|
| `shap_importance.csv` | per input: rank, mean absolute SHAP value, share of the total, direction (the rank correlation) and missing share |
| `shap_bar.png`, `shap_beeswarm.png` | the inputs with the largest shares; the beeswarm also shows every row's push against the input's value |
| `shap_examples.csv` | the three example days: unit, date, probability, prediction, outcome and the 5 largest pushes |
| `shap_example_true_alarm.png`, `shap_example_false_alarm.png`, `shap_example_missed_surge.png` | one waterfall chart per example day |
| `hp_importance.csv` | per model and setting: the rank and the fANOVA importance for the fold F1 and the inner log loss |
| `hp_importance_<model>.png` | each study's importances |
| `hp_slice_<model>.png` | every trial's fold F1 against the study's two most important settings |
| `sensitivity.json` | everything above, with the checks and the selection explained |
| `report.md` | one page: the checks, the top 10 inputs, the example days and the top tuning settings |

It also logs one MLflow run, `sensitivity-<model>`, in the experiment `model-sensitivity`:
- tags: the model, the selection, the data version, the model's run folder and MLflow run id, and the top input;
- params: the method, the rows explained, the inputs, the seed and the studies;
- metrics: both checks, every input's share and the top settings' importance;
- files: all 16 of the table's files.

**Where results live**

- **The `--out` folder:** `Model-Development/runs/sensitivity/` in the example above (gitignored), or `$A/reports/sensitivity/` in the image. The selection it explains is in `Model-Development/runs/selection/`, or `$A/reports/` in the image. Nothing is committed or stored in DVC, so run both again to remake them.
- **MLflow:** the server when `MLFLOW_TRACKING_URI` names it, else the local store `Model-Development/mlruns/` (`$A/mlruns/` in the image), as for training ([MLflow tracking server (M1)](#mlflow-tracking-server-m1)).

**Reading the results**

- **Associations, not causes.** A large share means the model leans on that input in the test data, not that the input causes 311 surges. Inputs that move together, like alert counts and forecast snowfall or the 311 lags of one storm group, share credit in ways the real world need not. `report.md` says so beside the numbers.
- **Pushes in log-odds.** A row's SHAP values plus the base value give its forecast's log-odds, so a push of +1 multiplies the odds of a surge by about 2.7.
- **Static inputs** (`static_*`) differ between neighborhoods, not between days, so they mostly tell the model which neighborhood a row belongs to.
- **It repeats.** On the same selection run, a rerun gives the same numbers and the same files byte for byte, except `shap_beeswarm.png`: shap draws its dots in a random order. Between the Mac and the training image (both on main):
  - **The SHAP numbers are identical:** `shap_importance.csv` and `shap_examples.csv` byte for byte, and the same SHAP and check blocks in `sensitivity.json`.
  - **The tuning importances depend on the platform:** up to 0.004 apart, in the same order. So `hp_importance.csv`, the LightGBM and XGBoost importance charts, and the tuning lines of `report.md` and `sensitivity.json` differ.
  - **The other charts, except the beeswarm, look the same but are different files:** they match pixel for pixel, and only their PNG compression differs.

**Current results** (data version `a719afeb`; the LightGBM winner of selection `20261114T045930Z-selection`, in the training image built from main; 13,104 scored test rows). The saved model gave back its run's test probabilities exactly, and the SHAP values added up to within 2.1e-15.

| Rank | Input | Share | Direction |
|---|---|---|---|
| 1 | `static_population` | 19.8% | higher value, higher forecast |
| 2 | `day_of_week` | 16.6% | higher value, lower forecast |
| 3 | `wx_tmax_c` | 11.9% | higher value, lower forecast |
| 4 | `snow_ice_rel_last` | 11.1% | higher value, higher forecast |
| 5 | `static_no_vehicle_share` | 7.8% | higher value, lower forecast |

- **True and false alarm:** the surest true alarm (North End, 2026-02-03, probability 0.891) and the surest false alarm (Back Bay, 2026-02-06, 0.860) were pushed up by the same inputs: `snow_ice_rel_last` first, then the cold (`wx_tmax_c`, `wx_tmin_c`).
- **Missed surge:** the one with the lowest probability (Longwood, 2025-04-28, 0.003) was pushed down mostly by `static_population` and `static_no_vehicle_share`.
- **Tuning settings:** the ones that mattered most for the fold F1 were `year_parts` for logistic regression (97.9% of its variation across trials), `max_depth` then `min_child_weight` for XGBoost (42.5%, 32.4%), and `colsample_bytree` then `learning_rate` for LightGBM (27.3%, 22.1%). These are the image's values, within 0.0037 of `importance.csv` and in the same order. On the Mac, where the studies ran, they equal `importance.csv`, with 42.4% and 32.5% for XGBoost.

**Code**

- `scripts/sensitivity.py`: the analysis.
- `tests/test_sensitivity.py` checks:
  - that the tree explainers' values add up, on LightGBM and XGBoost;
  - the permutation sample;
  - that the estimator is found inside a saved model;
  - the direction labels and the example days;
  - that it explains the winner, or else the best candidate;
  - the studies' importances, and a missing study;
  - the command end to end, with its MLflow run;
  - the stops on another data version and on a model that no longer gives back its probabilities.
- `shap==0.54.1`, pinned in `Data-Pipeline/requirements.txt` and `Model-Development/requirements.txt`.

The operating steps are in the runbook's [Sensitivity analysis (#113)](../docs/runbook.md#sensitivity-analysis-113).
