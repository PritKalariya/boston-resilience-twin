# Data Pipeline

The data pipeline for Boston Resilience Twin. It turns Boston's 311 requests into one table: storm-related requests per neighborhood, day and storm group from 2015 to today. That table feeds the surge labels and features (#16) and the models after them.

Everything here runs two ways: script by script on your own machine, or as one Airflow DAG (`daily_pipeline`) in Docker. Both download into `Data-Pipeline/data/raw/` and write the same files.

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

Every command below was run in a fresh clone on 2026-10-27 (macOS, Apple silicon, Docker Desktop with 8 GB). Times and row counts are from that run. The city adds new requests every day, so your counts will be a little higher. That run had no NOAA token or NWS user agent, so the three weather tasks that need them were marked success instead of run ([Run it in Airflow](#run-it-in-airflow) shows how).

## What the pipeline does

```
ingest_legacy_311 -> ingest_new_311     ingest_static_layers     ingest_noaa_logan, ingest_nws_forecasts,
  (Analyze Boston, 13 CSVs)               (tracts, neighborhoods,   ingest_nws_alerts, ingest_tides
          |                                population, flood zones)  (raw weather and tide history)
          |                                         |                          |
     validate_raw  <--------------------------------+--------------------------+
          |                                         |
  parse_legacy_311   parse_new_311                  |
  (one task a year)       |                         |
  merge_legacy_311        |                         |
          \             /                           |
           combine_311   (category map, switch dates)
                |                                   |
           spatial_join  <--------------------------+
                |
           daily_counts  ->  processed/daily_counts.parquet
                |
        validate_processed
                |
      schema_and_statistics  ->  processed/stats/
                |
             push_data   (DVC)
```

- **Two 311 systems.** The city is moving from the legacy system (yearly files since 2015) to a new one (one export, starting in 2025), category by category. `combine_311` maps both onto one set of categories with `docs/data/category-map.csv`, records each category's switch date in `docs/data/change-points.csv`, and drops legacy cases that were resubmitted in the new system. New-system times carry a `+00` suffix but are Boston local (ADR-020).
- **Placement.** `spatial_join` puts every request in a 2020 Census tract and its neighborhood by coordinates. The modeling unit is one of 24 tract-built neighborhoods (ADR-004). Requests with no coordinates, at the City Hall placeholder point, or outside Boston are kept and labeled, not dropped.
- **Counts.** `daily_counts` counts storm-related requests (snow and ice, flooding, trees) per neighborhood, day and storm group, fills days with no requests with 0, and adds the ADR-005 baseline counts. Legacy rows opened after their category's switch date are kept (ADR-021).
- **Downloads.** `ingest_311.py` keeps the 13 Analyze Boston files current on the schedule in `config/ingest_311_sources.csv` (2026 and the new system daily, 2025 weekly, older years monthly) and replaces a file only when its source changed. The four weather and tide tasks add to their history in `data/raw/weather/` and `data/raw/tides/` (#7); #16 turns them into features.
- **Checks.** Great Expectations runs on the raw files before parsing and on the processed tables after. A failed check fails the run and names the check.
- **Schema and statistics.** `schema_and_statistics` writes each processed table's schema and summary statistics, Great Expectations data docs, and `drift.json` with the shifts from the previous run (#35).
- **Alerts.** A failed task logs one `ALERT` line naming the task, the run and the error, and posts it to Slack when `SLACK_ALERT_WEBHOOK` is set (#34).

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

The 311 and static-layer steps need no key. Three of the weather tasks (#7) do, and without them the DAG run fails before `validate_raw`:

| Task | Needs |
|---|---|
| `ingest_noaa_logan` | `NOAA_CDO_TOKEN` |
| `ingest_nws_forecasts`, `ingest_nws_alerts` | `NWS_USER_AGENT` |

To get your own:

1. **NOAA CDO token:** request one at https://www.ncdc.noaa.gov/cdo-web/token. It comes by email within a few minutes.
2. **NWS user agent:** no sign-up. Any string with an app name and your email, for example `(boston-resilience-twin, you@example.org)`.
3. **Census key (optional):** request one at https://api.census.gov/data/key_signup.html, then activate it from the link in the email. No step needs it to run yet; `docs/data-access.md` has a request to check it.

`ingest_tides` needs no key. `SLACK_ALERT_WEBHOOK` is optional too (Run it in Airflow).

Copy the template and put each value in `.env`, in single quotes:

```bash
cp .env.example .env
```

```
NOAA_CDO_TOKEN='your-token'
NWS_USER_AGENT='(boston-resilience-twin, you@example.org)'
CENSUS_API_KEY='your-key'
```

`.env` is gitignored; never commit it. If the stack is already running, `docker compose up -d --wait` picks up the new values. A task whose key is missing fails at once, without retrying, and names it: `ingest_noaa_logan cannot start: NOAA_CDO_TOKEN not set (add to .env, ...)`. More detail, and one request per API to check that a key works: [docs/data-access.md](../docs/data-access.md).

## Get the raw data

**The 311 files.** Analyze Boston publishes one CSV per legacy year and one export for the new system. `ingest_311.py` looks up each file in the city's catalog by the resource id in `config/ingest_311_sources.csv` and downloads it to `Data-Pipeline/data/raw/311/`:

```bash
python Data-Pipeline/scripts/ingest_311.py
```

13 files, 1.7 GB, 82 seconds. It prints one line per file (`downloaded`, `updated` or `skipped`, with rows and the reason). A second run downloads only what is due and changed, so it is safe to repeat. The schedule state is in `Data-Pipeline/data/state/311/`. If you skip this, the DAG's `ingest_legacy_311` and `ingest_new_311` tasks do the same download on its first run. More options (`--dry-run`, `--only`, `--force`): runbook, "311 ingestion".

**The static layers** (tracts, neighborhoods, population, flood zones) download on the first run of `static_layers.py --download` in the next section, or of the `ingest_static_layers` task in Airflow.

**Weather and tides** download in the DAG (next sections), into `data/raw/weather/` and `data/raw/tides/`. The 311 steps below do not read them.

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
python Data-Pipeline/scripts/schema_stats.py
```

| Step | Writes | Time | Peak memory | Result |
|---|---|---|---|---|
| `static_layers.py --download` | `data/interim/static/`, `docs/data/static-layers.csv` | 19 s | 1.4 GB | 9 layers |
| `validate_data.py --stage raw` | `data/interim/validation/raw.json` | 27 s | 0.7 GB | 144 of 144 checks pass |
| `legacy_311.py` | `data/interim/legacy_311.parquet` | 30 s | 1.5 GB | 3,039,617 rows, 0 dropped |
| `new_311.py` | `data/interim/new_311.parquet` | 1 s | 0.2 GB | 77,112 rows, 0 dropped |
| `map_311.py` | `data/interim/311_combined.parquet`, `docs/data/change-points.csv` | 5 s | 2.2 GB | 3,115,423 rows |
| `spatial_join.py` | `data/interim/311_located.parquet` | 4 s | 2.2 GB | 99.1% placed in a neighborhood |
| `validate_data.py --stage processed` | `data/interim/validation/processed.json` | 10 s | 2.0 GB | 55 of 55 checks pass |
| `daily_counts.py` | `data/processed/daily_counts.parquet` | 2 s | 1.2 GB | 310,680 cells, 215,878 requests |
| `schema_stats.py` | `data/processed/stats/` | 8 s | 2.6 GB | schema, statistics, data docs, `drift.json` |

Each script prints a table you can check against the source: rows read and dropped per file, unmapped categories, placement by year, and how the daily total reconciles with the combined table, line by line. On a first run `schema_stats.py` prints `first run, nothing to compare with`; after that it lists the shifts from the previous run beyond the thresholds in `config/stats_thresholds.csv`. Open `data/processed/stats/data_docs/index.html` in a browser to see the statistics. Paths are under `Data-Pipeline/`. All of `Data-Pipeline/data/` is gitignored.

Two of these write tracked files in `docs/data/`. On fresh data, `git diff docs/data` shows the new retrieval dates in `static-layers.csv` (the file hashes match) and any switch date the city's newest records moved in `change-points.csv`. Keep the committed version with `git checkout docs/data` unless you mean to update it.

Then the tests and the lint, as CI runs them:

```bash
python -m pytest Data-Pipeline/tests
pip install ruff==0.17.2 && ruff check .
```

631 tests pass in 13 seconds. They never reach the network (`tests/conftest.py` fails any test that tries).

## Run it in Airflow

Start the stack from the repo root. On Linux, first set `AIRFLOW_UID` in `.env` to the output of `id -u`, so the files Airflow writes stay yours; macOS and Windows skip that.

```bash
docker compose up -d --build --wait
docker compose ps -a
docker compose exec airflow-scheduler airflow dags list-import-errors
```

The first start builds the Airflow image and took 1m47s. `docker compose ps -a` shows four services `(healthy)` and `airflow-init` as `Exited (0)`; `list-import-errors` prints `No data found`. The UI is at http://localhost:8080, login `airflow` / `airflow` (local defaults).

With the keys in `.env`, unpause the DAG and trigger the full build:

```bash
docker compose exec airflow-scheduler airflow dags unpause daily_pipeline
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r full-build
docker compose exec airflow-scheduler airflow dags list-runs daily_pipeline
docker compose exec airflow-scheduler airflow tasks states-for-dag-run daily_pipeline full-build
```

It writes to `Data-Pipeline/data/interim/` and `processed/`, the same files as the step-by-step run. If the 311 files are not there yet, the first run downloads them (82 seconds above). The legacy years parse side by side, at most 4 at a time; that change took the full build from 124 to 73 seconds ([Pipeline performance](../README.md#pipeline-performance-37) in the root README has the Gantt charts).

**Without the keys** a triggered run fails: `ingest_noaa_logan`, `ingest_nws_forecasts` and `ingest_nws_alerts` fail at once, and `validate_raw` and every task after it are `upstream_failed`. Checked on 2026-10-27. To run everything else, mark those three as successful with `dags test`, which runs the tasks one at a time in the scheduler container:

```bash
docker compose exec airflow-scheduler airflow dags test daily_pipeline --mark-success-pattern 'ingest_(noaa_logan|nws_forecasts|nws_alerts)$'
```

That is how the fresh-clone run was done: 142 seconds, run state `success`, with the raw 311 files already downloaded.

The DAG has 21 tasks. 18 run code: `check_range`; the downloads `ingest_legacy_311`, `ingest_new_311`, `ingest_static_layers`, `ingest_noaa_logan`, `ingest_nws_forecasts`, `ingest_nws_alerts` and `ingest_tides`; `validate_raw`; `parse_legacy_311` (one task instance per legacy year) and `merge_legacy_311`; `parse_new_311`, `combine_311`, `spatial_join`, `daily_counts`, `validate_processed`, `schema_and_statistics`; and `push_data`. The other 3 are placeholders until their issue lands: each logs that issue and succeeds (`PLANNED` in `scripts/pipeline_steps.py`).

| Placeholder | Issue |
|---|---|
| `ingest_open_meteo` | [#8](https://github.com/PritKalariya/boston-resilience-twin/issues/8): `open_meteo.py` runs by hand for now (runbook, "Weather download") |
| `labels_and_features` | [#16](https://github.com/PritKalariya/boston-resilience-twin/issues/16), in review in [PR #93](https://github.com/PritKalariya/boston-resilience-twin/pull/93) |
| `bias_slicing` | [#32](https://github.com/PritKalariya/boston-resilience-twin/issues/32) |

The `ingest_*` tasks retry twice on a network error, about 5 to 10 and then 10 to 15 minutes apart. A missing input or key fails a task at once, since a retry cannot fix it.

Each task logs one line with its run id, rows in and rows out, for example `run full-build task combine_311: rows in 3,116,729, rows out 3,115,423 in 7.6 s`. Logs, with everything each script printed, are in `Data-Pipeline/logs/dag_id=daily_pipeline/run_id=<run>/task_id=<task>/`. A failed task also logs `ALERT daily_pipeline.<task> failed in run <run> (try n): <error>`; with `SLACK_ALERT_WEBHOOK` in `.env` the same line goes to Slack (runbook, "Pipeline alerts (Slack)").

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

Without the keys, add `--mark-success-pattern` as above (`airflow dags test daily_pipeline -c '{"start": "2025-01-01", "end": "2025-12-31"}' --mark-success-pattern ...`); that run took 31 seconds, state `success`, 26,280 cells in `daily_counts`. The downloads are the same for every range, so a slice refreshes `data/raw/` too. In the UI, Trigger opens a form with the two dates. `start` must be 2015-01-01 or later and `end` on or after it; an empty `end` means through the latest data. A bad range fails at `check_range`. Delete the slice's folder when you are done.

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
│   ├── ingest_311.py            download the 311 files on their schedule
│   ├── static_layers.py         download and reproject the static layers
│   ├── noaa_logan.py            NOAA daily observations at Logan (#7)
│   ├── nws.py                   NWS forecasts and alerts (#7)
│   ├── tides.py                 Boston tide gauge, observed and predicted (#7)
│   ├── raw_fetch.py             shared download helpers for the weather scripts
│   ├── open_meteo.py            Open-Meteo history and archived forecasts (#8), by hand
│   ├── legacy_311.py            parse the legacy yearly files
│   ├── new_311.py               parse the new-system export
│   ├── map_311.py               category map, switch dates, combine both systems
│   ├── spatial_join.py          place requests in tracts and neighborhoods
│   ├── daily_counts.py          neighborhood x day x storm group counts
│   ├── validate_data.py         Great Expectations checks, raw and processed
│   ├── schema_stats.py          schema, statistics and drift of the processed tables
│   ├── alerts.py                failure alert line, to the log and Slack
│   ├── credentials.py           read API keys from the environment
│   ├── dvc_push.py              dvc push, skipped without bucket access
│   ├── after_switch.py          analysis behind ADR-021
│   ├── live_baselines.py        analysis behind ADR-005
│   ├── compare_units.py         analysis behind ADR-004
│   └── measure_311.py           reconciliation with the published counts
├── config/                      every tunable value, changed only by reviewed PR
│   ├── ingest_311_sources.csv     311 resource ids and download schedule
│   ├── published_311_counts.csv   published row counts of the 311 files
│   ├── static_layers.csv          sources of the static layers
│   ├── open_meteo.csv             Open-Meteo sets
│   ├── stats_thresholds.csv       drift thresholds for schema_stats
│   ├── placeholder_locations.csv  the City Hall placeholder point
│   ├── processing.csv             after_switch_policy (ADR-021)
│   └── validation_bounds.csv      Great Expectations bounds
├── tests/                       pytest, with small fixture files
├── data/                        gitignored: raw/, state/, interim/, processed/, slices/
├── logs/                        gitignored: Airflow task logs
├── dvc.yaml                     DVC stages (#18)
└── requirements.txt             pinned from the Airflow 3.3.2 constraints
```

The DAG holds no logic of its own. Each task calls `pipeline_steps.run_step`, which checks the inputs and keys, deletes the old outputs (except the downloads, which the ingest scripts replace only when the source changed), runs the script's `main()` with the arguments in `step_plan`, checks the outputs and logs the line above. So a script runs the same way from the shell and from Airflow, and the tests call the same functions.

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
- The static layers are checked by SHA-256 in `docs/data/static-layers.csv`; a fresh download on 2026-10-27 matched every hash.
- `ingest_311.py` records each file's SHA-256 and row count in `data/state/311/_manifest.json`, and a run with nothing new leaves `data/raw/311/` byte for byte as it was.
- The 311 files grow every day, so a fresh download is a newer version, not the same bytes. The full fresh-clone test on another machine is [#42](https://github.com/PritKalariya/boston-resilience-twin/issues/42).

## Evaluation criteria

| Criterion | Where | State |
|---|---|---|
| Proper documentation | this README, the [root README](../README.md), [runbook](../docs/runbook.md), [data card](../docs/data/data-card.md), [ADRs](../docs/decisions/) | done |
| Modular syntax and code | [Code structure](#code-structure): one module per step, `step_plan` for arguments | done |
| Pipeline orchestration (Airflow DAGs) | [Run it in Airflow](#run-it-in-airflow), `dags/daily_pipeline.py` | done: 311, static, weather and tide downloads run in the DAG (#6, #7); placeholders for #8, #16, #32 |
| Tracking and logging | the per-task log line in [Run it in Airflow](#run-it-in-airflow) (#36) | done |
| Data version control (DVC) | [DVC and reproducibility](#dvc-and-reproducibility) | remote and push task done (#19); stages [#18](https://github.com/PritKalariya/boston-resilience-twin/issues/18), history [#40](https://github.com/PritKalariya/boston-resilience-twin/issues/40) |
| Pipeline flow optimization (Gantt) | [Pipeline performance](../README.md#pipeline-performance-37) in the root README | done (#37): legacy years in parallel, full build 124 s to 73 s |
| Schema and statistics generation | `schema_stats.py` in [Run it step by step](#run-it-step-by-step); runbook, "Schema and statistics (every run)" | done (#35): every run, with data docs and `drift.json` |
| Anomaly detection and alerts | Great Expectations checks in [Run it step by step](#run-it-step-by-step); `ALERT` line and Slack in [Run it in Airflow](#run-it-in-airflow) | done (#17, #34) |
| Bias detection and mitigation | [data card](../docs/data/data-card.md), "Reported demand, not need" | [#32](https://github.com/PritKalariya/boston-resilience-twin/issues/32) slicing, [#33](https://github.com/PritKalariya/boston-resilience-twin/issues/33) mitigation |
| Test modules | `tests/`, 631 tests, run in CI on every PR (`.github/workflows/ci.yml`) | done; coverage audit [#38](https://github.com/PritKalariya/boston-resilience-twin/issues/38), in review: [PR #94](https://github.com/PritKalariya/boston-resilience-twin/pull/94) |
| Reproducibility | [Environment setup](#environment-setup) to [Small run](#small-run-one-year), [DVC and reproducibility](#dvc-and-reproducibility) | done here; second-machine test [#42](https://github.com/PritKalariya/boston-resilience-twin/issues/42) |
| Error handling and logging | runbook, "Run the data pipeline DAG": retries, input checks, `check_range` (#36) | done |

Owners update their row when their issue merges.
