# Model Development

The models for Boston Resilience Twin, and the tools they train and log with. Each component has a section here; the operating steps are in [docs/runbook.md](../docs/runbook.md).

1. [MLflow tracking server (M1)](#mlflow-tracking-server-m1)
2. [Hyperparameter tuning with Optuna (M6)](#hyperparameter-tuning-with-optuna-m6)
3. [Sensitivity analysis: SHAP and hyperparameter importance (M10)](#sensitivity-analysis-shap-and-hyperparameter-importance-m10)

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
