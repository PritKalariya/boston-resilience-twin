# Data Pipeline

The data pipeline for Boston Resilience Twin. It turns Boston's 311 requests into one table: storm-related requests per neighborhood, day and storm group from 2015 to today. That table feeds the surge labels and features (#16) and the models after them.

Everything here runs two ways: script by script on your own machine, or as one Airflow DAG (`daily_pipeline`) in Docker. Both read `Data-Pipeline/data/raw/` and write the same files.

Contents:

1. [What the pipeline does](#what-the-pipeline-does)
2. [Environment setup](#environment-setup)
3. [Get the raw data](#get-the-raw-data)
4. [Run it step by step](#run-it-step-by-step)
5. [Run it in Airflow](#run-it-in-airflow)
6. [Small run: one year](#small-run-one-year)
7. [Code structure](#code-structure)
8. [DVC and reproducibility](#dvc-and-reproducibility)
9. [Evaluation criteria](#evaluation-criteria)

Every command below was run in a fresh clone on 2026-10-26 (macOS, Apple silicon, Docker Desktop with 8 GB). Times and row counts are from that run. The city adds new requests every day, so your counts will be a little higher.

## What the pipeline does

```
Analyze Boston 311 CSVs (13 files)      static layers (tracts, neighborhoods, population)
          |                                         |
     validate_raw  <--------------------------------+
          |                                         |
  parse_legacy_311   parse_new_311                  |
          \             /                           |
           combine_311   (category map, switch dates)
                |                                   |
           spatial_join  <--------------------------+
                |
           daily_counts  ->  processed/daily_counts.parquet
                |
        validate_processed
                |
             push_data   (DVC)
```

- **Two 311 systems.** The city is moving from the legacy system (yearly files since 2015) to a new one (one export, starting in 2025), category by category. `combine_311` maps both onto one set of categories with `docs/data/category-map.csv`, records each category's switch date in `docs/data/change-points.csv`, and drops legacy cases that were resubmitted in the new system. New-system times carry a `+00` suffix but are Boston local (ADR-020).
- **Placement.** `spatial_join` puts every request in a 2020 Census tract and its neighborhood by coordinates. The modeling unit is one of 24 tract-built neighborhoods (ADR-004). Requests with no coordinates, at the City Hall placeholder point, or outside Boston are kept and labeled, not dropped.
- **Counts.** `daily_counts` counts storm-related requests (snow and ice, flooding, trees) per neighborhood, day and storm group, fills days with no requests with 0, and adds the ADR-005 baseline counts. Legacy rows opened after their category's switch date are kept (ADR-021).
- **Checks.** Great Expectations runs on the raw files before parsing and on the processed tables after. A failed check fails the run and names the check.

## Environment setup

You need:

- Python 3.12 (ADR-013). [uv](https://docs.astral.sh/uv/) creates a 3.12 venv if you do not have one.
- Docker Desktop, or Docker Engine 25+ with the Compose plugin, for the Airflow run. Give Docker at least 4 GB of memory; 8 GB is better.
- About 2.5 GB of free disk for the raw and processed data.
- A bash shell. On Windows, use WSL or Git Bash.

Clone the repo and create the venv from its root:

```bash
git clone https://github.com/PritKalariya/boston-resilience-twin.git
cd boston-resilience-twin
uv venv --python 3.12 --seed .venv && source .venv/bin/activate
pip install -r Data-Pipeline/requirements.txt
```

The install took 34 seconds. The pins come from the Airflow 3.3.2 constraints for Python 3.12, so the same versions run on the host and in the containers (ADR-022).

### API keys

The steps that run today need no key. The weather ingestion tasks (#7) need a NOAA token, a Census key and an NWS user agent. To get your own:

1. **NOAA CDO token:** request one at https://www.ncdc.noaa.gov/cdo-web/token. It comes by email within a few minutes.
2. **Census key:** request one at https://api.census.gov/data/key_signup.html, then activate it from the link in the email.
3. **NWS user agent:** no sign-up. Any string with an app name and your email, for example `(boston-resilience-twin, you@example.org)`.

Copy the template and put each value in `.env`, in single quotes:

```bash
cp .env.example .env
```

```
NOAA_CDO_TOKEN='your-token'
CENSUS_API_KEY='your-key'
NWS_USER_AGENT='(boston-resilience-twin, you@example.org)'
```

`.env` is gitignored; never commit it. If the stack is already running, `docker compose up -d --wait` picks up the new values. A task that needs a missing key fails and names it; every other task still runs. More detail, and one request per API to check that a key works: [docs/data-access.md](../docs/data-access.md).

## Get the raw data

**The 311 files.** Analyze Boston publishes one CSV per legacy year and one export for the new system. Their download links change each time the city refreshes a file, so this loop looks up each file's current link by its resource id in `config/published_311_counts.csv`:

```bash
mkdir -p Data-Pipeline/data/raw/311
tail -n +2 Data-Pipeline/config/published_311_counts.csv | while IFS=, read -r file rows checked id; do
  url=$(curl -fsS "https://data.boston.gov/api/3/action/resource_show?id=$id" | python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["url"])')
  curl -fsSL --retry 3 -o "Data-Pipeline/data/raw/311/$file" "$url"
done
```

13 files, 1.7 GB, about 2 minutes. The 311 ingestion DAG (#6) will replace this by hand step.

**The static layers** (tracts, neighborhoods, population, flood zones) download on the first run of `static_layers.py --download` in the next section, or of the `ingest_static_layers` task in Airflow.

## Run it step by step

From the repo root, with the venv active:

```bash
python Data-Pipeline/scripts/static_layers.py --download
python Data-Pipeline/scripts/validate_data.py --stage raw
python Data-Pipeline/scripts/legacy_311.py
python Data-Pipeline/scripts/new_311.py
python Data-Pipeline/scripts/map_311.py
python Data-Pipeline/scripts/spatial_join.py
python Data-Pipeline/scripts/validate_data.py --stage processed
python Data-Pipeline/scripts/daily_counts.py
```

| Step | Writes | Time | Peak memory | Result |
|---|---|---|---|---|
| `static_layers.py --download` | `data/interim/static/`, `docs/data/static-layers.csv` | 26 s | 1.4 GB | 9 layers |
| `validate_data.py --stage raw` | `data/interim/validation/raw.json` | 27 s | 1.0 GB | 144 of 144 checks pass |
| `legacy_311.py` | `data/interim/legacy_311.parquet` | 27 s | 1.9 GB | 3,039,560 rows, 0 dropped |
| `new_311.py` | `data/interim/new_311.parquet` | 1 s | 0.2 GB | 76,419 rows, 0 dropped |
| `map_311.py` | `data/interim/311_combined.parquet`, `docs/data/change-points.csv` | 6 s | 2.4 GB | 3,114,673 rows |
| `spatial_join.py` | `data/interim/311_located.parquet` | 4 s | 2.2 GB | 99.1% placed in a neighborhood |
| `validate_data.py --stage processed` | `data/interim/validation/processed.json` | 12 s | 2.0 GB | 55 of 55 checks pass |
| `daily_counts.py` | `data/processed/daily_counts.parquet` | 2 s | 1.2 GB | 310,608 cells, 215,867 requests |

Each script prints a table you can check against the source: rows read and dropped per file, unmapped categories, placement by year, and how the daily total reconciles with the combined table, line by line. Paths are under `Data-Pipeline/`. All of `Data-Pipeline/data/` is gitignored.

Two of these write tracked files in `docs/data/`. On fresh data, `git diff docs/data` shows the new retrieval dates in `static-layers.csv` (the file hashes match) and any switch date the city's newest records moved in `change-points.csv`. Keep the committed version with `git checkout docs/data` unless you mean to update it.

Then the tests and the lint, as CI runs them:

```bash
python -m pytest Data-Pipeline/tests
pip install ruff==0.17.2 && ruff check .
```

320 tests pass in 6 seconds. They never reach the network (`tests/conftest.py` fails any test that tries).

## Run it in Airflow

Start the stack from the repo root. On Linux, first set `AIRFLOW_UID` in `.env` to the output of `id -u`, so the files Airflow writes stay yours; macOS and Windows skip that.

```bash
docker compose up -d --build --wait
docker compose ps -a
docker compose exec airflow-scheduler airflow dags list-import-errors
```

The first start builds the Airflow image and took 1m47s. `docker compose ps -a` shows four services `(healthy)` and `airflow-init` as `Exited (0)`; `list-import-errors` prints `No data found`. The UI is at http://localhost:8080, login `airflow` / `airflow` (local defaults).

The DAG reads the raw files from the previous section. Unpause it, then trigger the full build:

```bash
docker compose exec airflow-scheduler airflow dags unpause daily_pipeline
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r full-build
docker compose exec airflow-scheduler airflow dags list-runs daily_pipeline
docker compose exec airflow-scheduler airflow tasks states-for-dag-run daily_pipeline full-build
```

The full build took 111 seconds, all 20 tasks `success`. It writes to `Data-Pipeline/data/interim/` and `processed/`, the same files as the step-by-step run.

The DAG has 20 tasks. 10 run code: `check_range`, `ingest_static_layers`, `validate_raw`, `parse_legacy_311`, `parse_new_311`, `combine_311`, `spatial_join`, `daily_counts`, `validate_processed` and `push_data`. The other 10 are placeholders until their issue lands: each logs that issue and succeeds (`PLANNED` in `scripts/pipeline_steps.py`).

| Placeholder | Issue |
|---|---|
| `ingest_legacy_311`, `ingest_new_311` | [#6](https://github.com/PritKalariya/boston-resilience-twin/issues/6) |
| `ingest_noaa_logan`, `ingest_nws_forecasts`, `ingest_nws_alerts`, `ingest_tides` | [#7](https://github.com/PritKalariya/boston-resilience-twin/issues/7) |
| `ingest_open_meteo` | [#8](https://github.com/PritKalariya/boston-resilience-twin/issues/8) |
| `labels_and_features` | [#16](https://github.com/PritKalariya/boston-resilience-twin/issues/16) |
| `schema_and_statistics` | [#35](https://github.com/PritKalariya/boston-resilience-twin/issues/35) |
| `bias_slicing` | [#32](https://github.com/PritKalariya/boston-resilience-twin/issues/32) |

Each task logs one line with its run id, rows in and rows out, for example `run full-build task combine_311: rows in 3,115,979, rows out 3,114,673 in 6.5 s`. Logs, with everything each script printed, are in `Data-Pipeline/logs/dag_id=daily_pipeline/run_id=<run>/task_id=<task>/`.

To stop the stack:

```bash
docker compose down
```

## Small run: one year

The DAG takes two params, `start` and `end`, both inclusive. Any range other than the full one is a slice, written to `Data-Pipeline/data/slices/<start>_<end>/` so it never overwrites the full build:

```bash
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r slice-2025 -c '{"start": "2025-01-01", "end": "2025-12-31"}'
docker compose exec airflow-scheduler airflow tasks states-for-dag-run daily_pipeline slice-2025
```

The 2025 slice took 45 seconds, all 20 tasks `success`. In the UI, Trigger opens a form with the two dates. `start` must be 2015-01-01 or later and `end` on or after it; an empty `end` means through the latest data. A bad range fails at `check_range`. Delete the slice's folder when you are done.

The counts step takes the same range on the host (2 seconds, 26,280 cells):

```bash
python Data-Pipeline/scripts/daily_counts.py --start 2025-01-01 --end 2025-12-31 --out Data-Pipeline/data/slices/daily_counts_2025.parquet
```

## Code structure

```
Data-Pipeline/
├── dags/
│   ├── daily_pipeline.py        the pipeline DAG: one task per step
│   └── hello.py                 smoke test for the stack
├── scripts/                     one module per step, each also a CLI
│   ├── pipeline_steps.py        each step's inputs, outputs and arguments; run_step, log line
│   ├── static_layers.py         download and reproject the static layers
│   ├── legacy_311.py            parse the legacy yearly files
│   ├── new_311.py               parse the new-system export
│   ├── map_311.py               category map, switch dates, combine both systems
│   ├── spatial_join.py          place requests in tracts and neighborhoods
│   ├── daily_counts.py          neighborhood x day x storm group counts
│   ├── validate_data.py         Great Expectations checks, raw and processed
│   ├── alerts.py                failure alert line (Slack and email with #34)
│   ├── credentials.py           read API keys from the environment
│   ├── dvc_push.py              dvc push, skipped without bucket access
│   ├── after_switch.py          analysis behind ADR-021
│   ├── live_baselines.py        analysis behind ADR-005
│   ├── compare_units.py         analysis behind ADR-004
│   └── measure_311.py           reconciliation with the published counts
├── config/                      every tunable value, changed only by reviewed PR
│   ├── published_311_counts.csv   row counts and resource ids of the 311 files
│   ├── static_layers.csv          sources of the static layers
│   ├── placeholder_locations.csv  the City Hall placeholder point
│   ├── processing.csv             after_switch_policy (ADR-021)
│   └── validation_bounds.csv      Great Expectations bounds
├── tests/                       pytest, with small fixture files
├── data/                        gitignored: raw/, interim/, processed/, slices/
├── logs/                        gitignored: Airflow task logs
├── dvc.yaml                     DVC stages (#18)
└── requirements.txt             pinned from the Airflow 3.3.2 constraints
```

The DAG holds no logic of its own. Each task calls `pipeline_steps.run_step`, which checks the inputs, deletes the old outputs, runs the script's `main()` with the arguments in `step_plan`, checks the outputs and logs the line above. So a script runs the same way from the shell and from Airflow, and the tests call the same functions.

Outside this folder: [docker-compose.yaml](../docker-compose.yaml) and [airflow.Dockerfile](../airflow.Dockerfile) for the stack, [docs/data/](../docs/data/) for the category map, switch dates and [data card](../docs/data/data-card.md), [docs/decisions/](../docs/decisions/) for the ADRs and [docs/runbook.md](../docs/runbook.md) for operating steps and fixes.

## DVC and reproducibility

**DVC.** The remote is `gs://boston-resilience-twin-data/dvc`, set in `.dvc/config` (#19):

```bash
dvc remote list
```

prints `gcs  gs://boston-resilience-twin-data/dvc  (default)`. `push_data`, the last task of the DAG, runs `scripts/dvc_push.py`. Without access to the team bucket, a grader's own machine included, it logs `dvc push skipped: no Google credentials ...` and succeeds, so the run does not fail on it:

```bash
docker compose exec airflow-scheduler airflow tasks test daily_pipeline push_data
```

Team members log in once with `gcloud auth application-default login`, then `dvc pull` fetches the data the current commit points to and `python Data-Pipeline/scripts/dvc_push.py` pushes. The stages that put the raw and processed files under DVC come with [#18](https://github.com/PritKalariya/boston-resilience-twin/issues/18), and their history with [#40](https://github.com/PritKalariya/boston-resilience-twin/issues/40). Until then the data is rebuilt from the public sources as above.

**Reproducibility.**

- Python 3.12 and every package pinned (ADR-013, ADR-022); the Airflow image is built from the same `requirements.txt`.
- Every tunable value is a reviewed file in `config/` or `docs/data/`, not a constant in code.
- Each script prints counts you can check against the source, and the counts step asserts that its total reconciles with the combined table.
- Re-running a step deletes its old outputs first, so a file left by an earlier run cannot pass for new output.
- The static layers are checked by SHA-256 in `docs/data/static-layers.csv`; a fresh download on 2026-10-26 matched every hash.
- The 311 files grow every day, so a fresh download is a newer version, not the same bytes. The full fresh-clone test on another machine is [#42](https://github.com/PritKalariya/boston-resilience-twin/issues/42).

## Evaluation criteria

| Criterion | Where | State |
|---|---|---|
| Proper documentation | this README, the [root README](../README.md), [runbook](../docs/runbook.md), [data card](../docs/data/data-card.md), [ADRs](../docs/decisions/) | done |
| Modular syntax and code | [Code structure](#code-structure): one module per step, `step_plan` for arguments | done |
| Pipeline orchestration (Airflow DAGs) | [Run it in Airflow](#run-it-in-airflow), `dags/daily_pipeline.py` | done; ingestion tasks are placeholders until #6, #7, #8 |
| Tracking and logging | the per-task log line in [Run it in Airflow](#run-it-in-airflow) (#36) | done |
| Data version control (DVC) | [DVC and reproducibility](#dvc-and-reproducibility) | remote and push task done (#19); stages [#18](https://github.com/PritKalariya/boston-resilience-twin/issues/18), history [#40](https://github.com/PritKalariya/boston-resilience-twin/issues/40) |
| Pipeline flow optimization (Gantt) | runbook, "Run the data pipeline DAG" | [#37](https://github.com/PritKalariya/boston-resilience-twin/issues/37), in review: [PR #80](https://github.com/PritKalariya/boston-resilience-twin/pull/80) parses the legacy years side by side |
| Schema and statistics generation | runbook, "Data validation" | [#35](https://github.com/PritKalariya/boston-resilience-twin/issues/35), in review: [PR #85](https://github.com/PritKalariya/boston-resilience-twin/pull/85) |
| Anomaly detection and alerts | Great Expectations checks in [Run it step by step](#run-it-step-by-step); failure alert line (`scripts/alerts.py`) | checks done (#17); Slack and email [#34](https://github.com/PritKalariya/boston-resilience-twin/issues/34), in review: [PR #89](https://github.com/PritKalariya/boston-resilience-twin/pull/89) |
| Bias detection and mitigation | [data card](../docs/data/data-card.md), "Reported demand, not need" | [#32](https://github.com/PritKalariya/boston-resilience-twin/issues/32) slicing, [#33](https://github.com/PritKalariya/boston-resilience-twin/issues/33) mitigation |
| Test modules | `tests/`, 320 tests, run in CI on every PR (`.github/workflows/ci.yml`) | done; coverage audit [#38](https://github.com/PritKalariya/boston-resilience-twin/issues/38) |
| Reproducibility | [Environment setup](#environment-setup) to [Small run](#small-run-one-year), [DVC and reproducibility](#dvc-and-reproducibility) | done here; second-machine test [#42](https://github.com/PritKalariya/boston-resilience-twin/issues/42) |
| Error handling and logging | runbook, "Run the data pipeline DAG": retries, input checks, `check_range` (#36) | done |

Owners update their row when their issue merges.
