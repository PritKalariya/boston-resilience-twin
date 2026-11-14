# Model Development

The models for Boston Resilience Twin, and the tools they train and log with. Each component has a section here; the operating steps are in [docs/runbook.md](../docs/runbook.md).

1. [MLflow tracking server (M1)](#mlflow-tracking-server-m1)
2. [Sensitivity analysis: SHAP and hyperparameter importance (M10)](#sensitivity-analysis-shap-and-hyperparameter-importance-m10)

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
