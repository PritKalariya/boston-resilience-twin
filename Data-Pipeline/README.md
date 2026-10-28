# Data Pipeline

The data pipeline for Boston Resilience Twin. It turns Boston's 311 requests, weather, tides and NWS alerts into the tables the models train on: storm-related requests per neighborhood, day and storm group from 2015 to today, a surge label for each neighborhood and day, and a feature table that uses only what was known by 6 PM the evening before (#16).

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
9. [Bias detection and mitigation](#bias-detection-and-mitigation)
10. [Evaluation criteria](#evaluation-criteria)

Every command below was run in a fresh clone on 2026-10-27 (macOS, Apple silicon, Docker Desktop with 8 GB). Times and row counts are from that run. The city adds new requests every day, so your counts will be a little higher. That run had no NOAA token or NWS user agent, so the three weather tasks that need them, and `feature_table`, which reads the NOAA download, were marked success instead of run ([Run it in Airflow](#run-it-in-airflow) shows how).

## What the pipeline does

```
ingest_legacy_311 -> ingest_new_311     ingest_static_layers     ingest_noaa_logan, ingest_tides,
  (Analyze Boston, 13 CSVs)               (tracts, neighborhoods,   ingest_open_meteo, ingest_alert_history,
          |                                population, flood zones)  ingest_nws_forecasts, ingest_nws_alerts
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
            /       \
  surge_labels     feature_table   ->  processed/labels.parquet, features.parquet
            \       /
        validate_processed
            /       \
  schema_and_statistics   bias_slicing   ->  processed/stats/, interim/reports/bias/
            \       /
             push_data   (DVC)
```

- **Two 311 systems.** The city is moving from the legacy system (yearly files since 2015) to a new one (one export, starting in 2025), category by category. `combine_311` maps both onto one set of categories with `docs/data/category-map.csv`, records each category's switch date in `docs/data/change-points.csv`, and drops legacy cases that were resubmitted in the new system. New-system times carry a `+00` suffix but are Boston local (ADR-020).
- **Placement.** `spatial_join` puts every request in a 2020 Census tract and its neighborhood by coordinates. The modeling unit is one of 24 tract-built neighborhoods (ADR-004). Requests with no coordinates, at the City Hall placeholder point, or outside Boston are kept and labeled, not dropped.
- **Counts.** `daily_counts` counts storm-related requests (snow and ice, flooding, trees) per neighborhood, day and storm group, fills days with no requests with 0, and adds the ADR-005 baseline counts. Legacy rows opened after their category's switch date are kept (ADR-021).
- **Downloads.** `ingest_311.py` keeps the 13 Analyze Boston files current on the schedule in `config/ingest_311_sources.csv` (2026 and the new system daily, 2025 weekly, older years monthly) and replaces a file only when its source changed. The weather, tide and alert tasks add to their history in `data/raw/weather/`, `data/raw/tides/`, `data/raw/open_meteo/` and `data/raw/nws_alerts/` (#7, #8, ADR-014).
- **Checks.** Great Expectations runs on the raw files before parsing and on the processed tables after. A failed check fails the run and names the check.
- **Labels and features.** `surge_labels` marks a neighborhood-day as a surge when its storm requests are well above the same weeks of the 3 years before (ADR-005; rules in `config/surge_rules.csv`). `feature_table` builds one row per neighborhood and day from 311 lags (raw and self-relative, #33), NWS alerts from the Iowa Environmental Mesonet archive (ADR-014), observed weather at Logan to 2023, day-before forecasts from 2024 (ADR-003), predicted tides, the calendar and the static layers, each cut at 6 PM the evening before. Details: [docs/feature-table.md](../docs/feature-table.md).
- **Bias slicing.** `bias_slicing` compares requests per capita, missing locations, surge rates and label coverage across slices: each neighborhood against the rest, ACS poverty, race and ethnicity and limited-English shares above or below the median neighborhood, the flood zone, and the legacy against the new 311 system (#32). It uses Fairlearn's `MetricFrame` and writes `slices.csv`, `gaps.csv` and `report.md` to `data/interim/reports/bias/`; runbook, "Bias slicing (#32)". The reviewed results are in [reports/bias/report.md](reports/bias/report.md): 105 of 314 gaps significant (four-fifths ratio under 0.8 and p under 0.01). Mitigation, with before and after numbers, is in [Bias detection and mitigation](#bias-detection-and-mitigation) (#33).
- **Schema and statistics.** `schema_and_statistics` writes each processed table's schema and summary statistics, Great Expectations data docs, and `drift.json` with the shifts from the previous run (#35).
- **Alerts.** A failed task logs one `ALERT` line naming the task, the run and the error, and posts it to Slack when `SLACK_ALERT_WEBHOOK` is set (#34).

## Environment setup

You need:

- Python 3.12 (ADR-013). [uv](https://docs.astral.sh/uv/) creates a 3.12 venv if you do not have one.
- Docker Desktop, or Docker Engine 25+ with the Compose plugin, for the Airflow run. Give Docker at least 4 GB of memory; 8 GB is better. A task killed for memory shows `SIGKILL` or exit code `-9` or `137` in its log; raise Docker's memory or put `LEGACY_PARALLEL=2` in `.env`.
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

Most steps need no key. Three weather tasks (#7) do, and without them the DAG run fails before `validate_raw`:

| Task | Needs |
|---|---|
| `ingest_noaa_logan` | `NOAA_CDO_TOKEN` |
| `ingest_nws_forecasts`, `ingest_nws_alerts` | `NWS_USER_AGENT` |

`feature_table` reads the NOAA download, so it needs the token too.

To get your own:

1. **NOAA CDO token:** request one at https://www.ncdc.noaa.gov/cdo-web/token. It comes by email within a few minutes.
2. **NWS user agent:** no sign-up. Any string with an app name and your email, for example `(boston-resilience-twin, you@example.org)`.
3. **Census key (optional):** request one at https://api.census.gov/data/key_signup.html, then activate it from the link in the email. No step calls the Census API: the ACS figures the bias slices use come from Analyze Boston's population tables (`config/static_layers.csv`). `docs/data-access.md` has a request to check the key.

`ingest_tides`, `ingest_open_meteo` and `ingest_alert_history` need no key. `SLACK_ALERT_WEBHOOK` is optional too (Run it in Airflow).

Copy the template and put each value in `.env`, in single quotes:

```bash
cp .env.example .env
```

On Linux, also set your user id, so the files Airflow writes stay yours. This edits the one line and keeps the rest of `.env` (macOS and Windows skip it):

```bash
sed -i "s/^AIRFLOW_UID=.*/AIRFLOW_UID=$(id -u)/" .env
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

**Weather, tides and alerts** download in the DAG (next sections). The 311 steps below do not read them; the feature table does.

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
python Data-Pipeline/scripts/pipeline_steps.py surge_labels
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
| `pipeline_steps.py surge_labels` | `data/processed/labels.parquet` | 6 s | 0.3 GB | 103,560 cells, 94,680 labeled, 3,557 surges (3.76%) |

`pipeline_steps.py <step>` runs any DAG step for the full build with the DAG's own paths and checks; it is what the DVC stages call. The feature table runs the same way (`pipeline_steps.py feature_table`) once the weather, tide and alert downloads are there, which needs `NOAA_CDO_TOKEN`; without it the step stops with `feature_table cannot start: missing .../raw/weather/noaa_logan`.

If `static_layers.py --download` stops with `... could not be read as a vector layer`, the open data portal sent a short reply instead of the file. Delete that file in `Data-Pipeline/data/raw/static/` and run `python Data-Pipeline/scripts/static_layers.py --download` again; the other layers are kept.

Each script prints a table you can check against the source: rows read and dropped per file, unmapped categories, placement by year, and how the daily total reconciles with the combined table, line by line. On a first run `schema_stats.py` prints `first run, nothing to compare with`; after that it lists the shifts from the previous run beyond the thresholds in `config/stats_thresholds.csv`. Open `data/processed/stats/data_docs/index.html` in a browser to see the statistics. Paths are under `Data-Pipeline/`. All of `Data-Pipeline/data/` is gitignored.

Two of these write tracked files in `docs/data/`. On fresh data, `git diff docs/data` shows the new retrieval dates in `static-layers.csv` (the file hashes match) and any switch date the city's newest records moved in `change-points.csv`. Keep the committed version with `git checkout docs/data` unless you mean to update it.

Then the tests and the lint, as CI runs them:

```bash
python -m pytest Data-Pipeline/tests
pip install ruff==0.17.2 && ruff check .
```

973 tests pass and 14 skip in 44 seconds; the skipped ones are the DAG tests, which need Airflow. To run all of them with coverage, in the Airflow image on Python 3.12 (#38):

```bash
docker compose run --rm --build test
```

986 passed and 1 skipped in 100 seconds, with 97.7% line coverage of `Data-Pipeline/scripts/`; the run fails below 70% (`.coveragerc`). They never reach the network (`tests/conftest.py` fails any test that tries).

## Run it in Airflow

Start the stack from the repo root. On Linux, set `AIRFLOW_UID` first ([API keys](#api-keys) has the `sed` line).

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

It writes to `Data-Pipeline/data/interim/` and `processed/`, the same files as the step-by-step run. If the 311 files are not there yet, the first run downloads them (82 seconds above). The first run also downloads the Open-Meteo history (92 seconds) and the alert history (14 seconds); later runs fetch only what is new. The legacy years parse side by side, at most 4 at a time; that change took the full build from 124 to 73 seconds ([Pipeline performance](../README.md#pipeline-performance-37) in the root README has the Gantt charts).

**Without the keys** a triggered run fails: `ingest_noaa_logan`, `ingest_nws_forecasts` and `ingest_nws_alerts` fail at once, and `validate_raw` and every task after it are `upstream_failed`. Marking only those three as successful is not enough: `feature_table` then stops with `feature_table cannot start: missing .../raw/weather/noaa_logan`. To run everything else, mark all four with `dags test`, which runs the tasks one at a time in the scheduler container. Pause the DAG first: while it is unpaused, the scheduler also picks up the run `dags test` creates, runs the keyed tasks itself, and they fail and leave `validate_raw` `upstream_failed`, so the test stops with `invalid_state`. It is a race, so it can pass on one machine and fail on another. The same goes for every `dags test` and `tasks test` command below; unpause only for a triggered run.

```bash
docker compose exec airflow-scheduler airflow dags pause daily_pipeline
docker compose exec airflow-scheduler airflow dags test daily_pipeline --mark-success-pattern '^(ingest_(noaa_logan|nws_forecasts|nws_alerts)|feature_table)$'
```

That is how the fresh-clone run was done on 2026-10-27: 227 seconds, run state `success`, with the downloads from an earlier run on disk. Every other task ran, `surge_labels` and `bias_slicing` included. That run also picked up the city's daily update: `ingest_311` logged `legacy_2026 updated 190904 190800` and replaced only `2026.csv` and `new_system.csv`.

The DAG has 23 tasks, and all of them run code: `check_range`; the downloads `ingest_legacy_311`, `ingest_new_311`, `ingest_static_layers`, `ingest_noaa_logan`, `ingest_nws_forecasts`, `ingest_nws_alerts`, `ingest_tides`, `ingest_open_meteo` and `ingest_alert_history`; `validate_raw`; `parse_legacy_311` (one task instance per legacy year) and `merge_legacy_311`; `parse_new_311`, `combine_311`, `spatial_join`, `daily_counts`, `surge_labels`, `feature_table`, `validate_processed`, `schema_and_statistics`, `bias_slicing`; and `push_data`. `PLANNED` in `scripts/pipeline_steps.py`, which held the placeholders, is now empty.

The `ingest_*` tasks retry twice on a network error, about 5 to 10 and then 10 to 15 minutes apart. A missing input or key fails a task at once, since a retry cannot fix it.

Each task logs one line with its run id, rows in and rows out, for example `run full-build task combine_311: rows in 3,116,729, rows out 3,115,423 in 6.4 s`. Logs, with everything each script printed, are in `Data-Pipeline/logs/dag_id=daily_pipeline/run_id=<run>/task_id=<task>/`. A failed task also logs `ALERT daily_pipeline.<task> failed in run <run> (try n): <error>`; with `SLACK_ALERT_WEBHOOK` in `.env` the same line goes to Slack (runbook, "Pipeline alerts (Slack)").

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

Without the keys, pause the DAG and add `--mark-success-pattern` as above (`airflow dags test daily_pipeline -c '{"start": "2025-01-01", "end": "2025-12-31"}' --mark-success-pattern ...`); that run took 45 seconds, state `success`: 26,280 cells in `daily_counts` and 8,760 in `surge_labels`. Labels and features read the full build's daily counts, since a label compares a day with the 3 years before it, so run the full build first. The downloads are the same for every range, so a slice refreshes `data/raw/` too. In the UI, Trigger opens a form with the two dates. `start` must be 2015-01-01 or later and `end` on or after it; an empty `end` means through the latest data. A bad range fails at `check_range`. Delete the slice's folder when you are done.

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
│   ├── open_meteo.py            Open-Meteo history and archived forecasts (#8)
│   ├── open_meteo_forecast.py   reads the archived day-before forecasts for the features
│   ├── period_files.py          which stored year and half-year files the feature readers use
│   ├── alert_history.py         NWS alert history from the Iowa Environmental Mesonet (ADR-014)
│   ├── legacy_311.py            parse the legacy yearly files
│   ├── new_311.py               parse the new-system export
│   ├── map_311.py               category map, switch dates, combine both systems
│   ├── spatial_join.py          place requests in tracts and neighborhoods
│   ├── daily_counts.py          neighborhood x day x storm group counts
│   ├── surge_labels.py          surge labels per neighborhood and day (#16)
│   ├── bias_slicing.py          bias slices and their tests (#32)
│   ├── bias_mitigation.py       before and after numbers for the self-relative lags (#33)
│   ├── feature_table.py         the cutoff-safe feature table (#16), with weather_features.py and tide_features.py
│   ├── validate_data.py         Great Expectations checks, raw and processed
│   ├── schema_stats.py          schema, statistics and drift of the processed tables
│   ├── alerts.py                failure alert line, to the log and Slack
│   ├── credentials.py           read API keys from the environment
│   ├── dvc_push.py              dvc push, skipped without bucket access
│   ├── dvc_stages.py            writes dvc.yaml from step_plan (#18)
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
│   ├── surge_rules.csv            surge label rules
│   ├── labels_features.csv        label and feature settings (cutoff, windows)
│   ├── feature_catalog.csv        every feature column and where it comes from
│   ├── bias_slicing.csv           slice thresholds and test settings
│   ├── placeholder_locations.csv  the City Hall placeholder point
│   ├── processing.csv             after_switch_policy (ADR-021)
│   └── validation_bounds.csv      Great Expectations bounds
├── reports/bias/                bias results: report.md, slices.csv, gaps.csv (#32); mitigation.md, mitigation.csv (#33)
├── tests/                       pytest, with small fixture files
├── data/                        gitignored: raw/, state/, interim/, processed/, slices/
├── logs/                        gitignored: Airflow task logs
├── dvc.yaml, dvc.lock           DVC stages, generated from step_plan (#18), and their hashes
└── requirements.txt             pinned from the Airflow 3.3.2 constraints
```

The DAG holds no logic of its own. Each task calls `pipeline_steps.run_step`, which checks the inputs and keys, deletes the old outputs (except the downloads, which the ingest scripts replace only when the source changed), runs the script's `main()` with the arguments in `step_plan`, checks the outputs and logs the line above. So a script runs the same way from the shell and from Airflow, and the tests call the same functions.

Outside this folder: [docker-compose.yaml](../docker-compose.yaml) and [airflow.Dockerfile](../airflow.Dockerfile) for the stack, [docs/data/](../docs/data/) for the category map, switch dates and [data card](../docs/data/data-card.md), [docs/decisions/](../docs/decisions/) for the ADRs and [docs/runbook.md](../docs/runbook.md) for operating steps and fixes.

## DVC and reproducibility

**DVC.** `Data-Pipeline/dvc.yaml` holds the pipeline's stages (#18, ADR-024): the same steps the DAG runs, for the full build, from the downloads to `processed/labels.parquet` and `processed/features.parquet`. `scripts/dvc_stages.py` writes it from `step_plan`, so each step is defined once, and every stage runs `python scripts/pipeline_steps.py <step>`, the same `run_step` the DAG task calls. `dvc.lock` records the hash of every input and output of the last full build. Run DVC from `Data-Pipeline/`:

```bash
cd Data-Pipeline
dvc remote list
dvc dag
dvc repro daily_counts
dvc repro daily_counts
```

`dvc remote list` prints `gcs  gs://boston-resilience-twin-data/dvc  (default)` (#19). On the fresh clone's data, the first `dvc repro daily_counts` ran the 20 stages it needs in 63 seconds, and the second printed `Data and pipelines are up to date.` **Without bucket access** (a grader), skip `dvc pull` and rebuild everything with `dvc repro`, which needs `NOAA_CDO_TOKEN` (about 2 GB of downloads; 630 seconds in Kirtan's clean-clone run). Your download is newer than the one `dvc.lock` pins (the 2026 and new-system 311 files change daily), so your row counts and hashes will differ a little from ours; that is new data, not a reproducibility failure. If `ingest_static_layers` stops with `... could not be read as a vector layer`, delete that file in `Data-Pipeline/data/raw/static/` and run `dvc repro` again; it resumes from that stage. `dvc repro` updates `dvc.lock` in your working copy; keep the committed one with `git checkout dvc.lock`.

`push_data`, the last task of the DAG, runs `scripts/dvc_push.py`. Without access to the team bucket, a grader's own machine included, it logs `dvc push skipped: no Google credentials ...` and succeeds, so the run does not fail on it. To run it alone, pause the DAG first, as for `dags test`:

```bash
docker compose exec airflow-scheduler airflow dags pause daily_pipeline
docker compose exec airflow-scheduler airflow tasks test daily_pipeline push_data
```

Team members log in once with `gcloud auth application-default login`; then, in `Data-Pipeline/`, `dvc pull` fetches the data the committed `dvc.lock` names and `dvc repro` prints `Data and pipelines are up to date.` A pull of two outputs in a clean clone (`dvc pull daily_counts surge_labels`) fetched both, 310,752 and 103,584 rows.

**Data versions (#40).** Each commit that changes `dvc.lock` is a data version: the first was `a86e6e5` (#18), then `0812b8b` (#94), and #98 refreshed it again. From `Data-Pipeline/`:

```bash
git log --oneline -- dvc.lock          # the data versions, newest first
dvc diff a86e6e5 0812b8b               # 49 files changed between the first two
git checkout a86e6e5 -- dvc.lock && dvc pull     # the previous version
git checkout HEAD -- dvc.lock && dvc checkout    # back to the current one
```

All four were run in a clean clone on 2026-10-27, with team credentials: the previous version pulled in 11 seconds and switching back took 1 second, ending with `Data and pipelines are up to date.` Labels are identical in the two versions; the features and the 311 tables differ, with the same row counts. A DAG run writes newer files but does not update `dvc.lock`: the next version is a DAG run (or the ingest steps) followed by `dvc repro`, a push and a commit of `dvc.lock`. The runbook, "Data versions (#40)", has the steps.

**Reproducibility.**

- Python 3.12 and every package pinned (ADR-013, ADR-022); the Airflow image is built from the same `requirements.txt`.
- Every tunable value is a reviewed file in `config/` or `docs/data/`, not a constant in code.
- Each script prints counts you can check against the source, and the counts step asserts that its total reconciles with the combined table.
- Re-running a step deletes its old outputs first, so a file left by an earlier run cannot pass for new output.
- The static layers are checked by SHA-256 in `docs/data/static-layers.csv`; a fresh download on 2026-10-27 matched every hash.
- `ingest_311.py` records each file's SHA-256 and row count in `data/state/311/_manifest.json`, and a run with nothing new leaves `data/raw/311/` byte for byte as it was.
- The 311 files grow every day, so a fresh download is a newer version, not the same bytes. The full fresh-clone test on another machine is [#42](https://github.com/PritKalariya/boston-resilience-twin/issues/42).

## Bias detection and mitigation

**Bias found (#32).** `bias_slicing` compares four measures across slices of the 24 neighborhoods: requests per capita, missing location, surge rate and label coverage. The slices are ACS poverty, people of color and limited-English shares above or below the median, the flood zone, and the 311 system. A tract check repeats requests per capita on the 207 tracts. 105 of 314 tests are significant ([reports/bias/report.md](reports/bias/report.md)). The main finding is about **reporting**: 311 counts reported demand, not need.

| slice | storm requests per 1,000 residents a year, 2015 to 2023 |
|---|---|
| poverty, neighborhoods | 20.5 higher-poverty vs 29.2 lower-poverty (0.70) |
| poverty, tracts | 17.8 vs 31.0 (0.57) |
| people of color, tracts | 21.2 vs 27.3 (0.78) |
| limited English, tracts | 21.2 vs 27.7 (0.76) |

The labels don't carry this gap: each neighborhood is compared with its own ADR-005 baseline, so higher-poverty neighborhoods surge about as often (3.40% against 3.76%). But 8 of the model's 31 inputs were raw 311 lag counts, so the features did carry it. The other gaps are:
- higher surge rates where more people of color or limited-English speakers live;
- very low surge rates in the smallest neighborhoods (Longwood, Chinatown, West End, South Boston Waterfront; Chinatown in 2024 and Harbor Islands with none);
- a jump in requests in the year the new 311 system took storm requests;
- missing locations, under 0.25% everywhere.

**How it was addressed (#33, [ADR-025](../docs/decisions/ADR-025-bias-mitigation.md)).**
- **Reporting: self-relative 311 lags.** `feature_table` adds `{group}_rel_last` and `{group}_rel3`, `rel7`, `rel28`: each lag over the neighborhood's own ADR-005 baseline mean, as (requests + 1) / (baseline + 1). A value of 1 is a usual day for that neighborhood, whatever it usually reports.
  - **Default inputs:** they're the default model inputs in `config/feature_catalog.csv`, and the raw lags stay in the table with `use_in_model = no`.
  - **Leakage:** the baseline uses only the same weeks of the 3 previous years, and `check_no_leakage` recomputes these features by date.
- **Labels:** documented, not changed. They're already self-relative; the worst-group recall gap (ADR-008) covers the model.
- **Small neighborhoods:** deferred to the surge rule freeze ADR, which sets the request floor m (m 5 against m 4, #16).
- **The 311 migration:** deferred to drift monitoring (ADR-008), with the switch dates. The two eras are different years, so the slice cannot separate the system from that year's weather.
- **Missing location:** documented. It's too small, and only partly measured.

**Before and after** (`scripts/bias_mitigation.py`, [reports/bias/mitigation.md](reports/bias/mitigation.md)):
- **What's compared:** each lag version's mean between the two groups of each slice (poverty, people of color, limited English, flood zone), per period, storm group and window.
- **Rows:** 90,091 labeled neighborhood-days, 3,554 of them surge days.
- **The rule:** a gap is a lower mean under 0.8 times the higher one.

| version | gaps, all labeled days | gaps, surge days |
|---|---|---|
| raw (before) | 52 of 96 | 53 of 96 |
| per capita | 45 of 96 | 64 of 96 |
| self-relative (now) | 0 of 96 | 7 of 96 |

The 7 left are surge days in 2024, the smallest sample. Both groups are above 1 there, so the storm still shows in each.

**Trade-offs.**
- **An input-level result.** Dividing by a neighborhood's own baseline puts it near 1 on an ordinary day by construction, so equal means on all days are partly expected; the surge-day numbers say more. Neither shows the model will be fairer or more accurate.
- **Size of a storm:** the model loses a storm's absolute size in requests. Population and density stay as features to recover scale, and the +1 makes tiny neighborhoods' values coarse.
- **Coverage:** the self-relative lags start just after the first labels, so 696 of 70,008 labeled training rows (0.99%, January 2016) miss at least one window. The raw lags miss none.
- **Rejected alternatives:**
  - per-capita lags keep the gap;
  - reweighting training rows leaves the features biased;
  - deferring everything to modeling gives no evidence before a model exists.
- **What Model Development checks** (ADR-025): it compares raw lags, self-relative lags, and self-relative lags plus the baseline level on 2024 validation recall per slice and the worst-group gap, and records the inputs it picks. Grading keeps watching the same groups.

## Evaluation criteria

| Criterion | Where | State |
|---|---|---|
| Proper documentation | this README, the [root README](../README.md), [runbook](../docs/runbook.md), [data card](../docs/data/data-card.md), [ADRs](../docs/decisions/) | done |
| Modular syntax and code | [Code structure](#code-structure): one module per step, `step_plan` for arguments | done |
| Pipeline orchestration (Airflow DAGs) | [Run it in Airflow](#run-it-in-airflow), `dags/daily_pipeline.py` | done: every step from the downloads (#6, #7, #8) to labels, features (#16) and bias slicing (#32) runs in the DAG; no placeholders left |
| Tracking and logging | the per-task log line in [Run it in Airflow](#run-it-in-airflow) (#36) | done |
| Data version control (DVC) | [DVC and reproducibility](#dvc-and-reproducibility), `dvc.yaml` and `dvc.lock` | done: remote and push task (#19), stages from the downloads to labels and features (#18), two data versions with checkout steps (#40) |
| Pipeline flow optimization (Gantt) | [Pipeline performance](../README.md#pipeline-performance-37) in the root README | done (#37): legacy years in parallel, full build 124 s to 73 s |
| Schema and statistics generation | `schema_stats.py` in [Run it step by step](#run-it-step-by-step); runbook, "Schema and statistics (every run)" | done (#35): every run, with data docs and `drift.json` |
| Anomaly detection and alerts | Great Expectations checks in [Run it step by step](#run-it-step-by-step); `ALERT` line and Slack in [Run it in Airflow](#run-it-in-airflow) | done (#17, #34) |
| Bias detection and mitigation | [Bias detection and mitigation](#bias-detection-and-mitigation); [reports/bias/report.md](reports/bias/report.md) (105 of 314 gaps significant) and [mitigation.md](reports/bias/mitigation.md); Bias slicing in [What the pipeline does](#what-the-pipeline-does); runbook, "Bias slicing (#32)"; [data card](../docs/data/data-card.md), "Reported demand, not need" | done: slicing ([#32](https://github.com/PritKalariya/boston-resilience-twin/issues/32)), the `bias_slicing` task and DVC stage; mitigation ([#33](https://github.com/PritKalariya/boston-resilience-twin/issues/33), [ADR-025](../docs/decisions/ADR-025-bias-mitigation.md)), self-relative 311 lags with before and after numbers in [Bias detection and mitigation](#bias-detection-and-mitigation) |
| Test modules | `tests/`, 986 tests with 97.7% coverage (`docker compose run --rm --build test`), run in CI on every PR (`.github/workflows/ci.yml`) | done (#38) |
| Reproducibility | [Environment setup](#environment-setup) to [Small run](#small-run-one-year), [DVC and reproducibility](#dvc-and-reproducibility) | done here; second-machine test [#42](https://github.com/PritKalariya/boston-resilience-twin/issues/42) |
| Error handling and logging | runbook, "Run the data pipeline DAG": retries, input checks, `check_range` (#36) | done |

Owners update their row when their issue merges.
