# Runbook

Operational steps for the Boston Resilience Twin. Update this file in the PR
that changes an operational step.

## Local Airflow stack (Docker Compose)

`docker-compose.yaml` at the repo root runs Airflow 3.3.2 with LocalExecutor
and a Postgres 16 metadata database: `postgres`, a one-shot `airflow-init`,
`airflow-apiserver` (UI and API on 127.0.0.1:8080 only), `airflow-scheduler`
(also runs the tasks), and `airflow-dag-processor`. There is no triggerer: add
the official triggerer service back when the first deferrable operator lands,
since a deferred task waits forever without one.

Every Airflow service runs `brt-airflow:local`, built from `airflow.Dockerfile`
at the repo root: the `apache/airflow:3.3.2-python3.12` base image (ADR-013)
plus `Data-Pipeline/requirements.txt` installed under Airflow's constraints for
that image (ADR-022). The requirements file is the only thing copied in; DAG
and script code is bind-mounted (ADR-017).

### Rebuild the image

```bash
docker compose up -d --build --wait
```

Run it after any change to `Data-Pipeline/requirements.txt`, including one that
arrives with a `git pull`. DAG and script changes never need a rebuild. With
the requirements unchanged the build is fully cached and adds about a second,
so it is safe to use `--build` on every start.

### Start and check

```bash
echo "AIRFLOW_UID=$(id -u)" > .env   # Linux only (the VM too), once per clone
docker compose up -d --build --wait
docker compose ps -a
curl -fsS http://127.0.0.1:8080/api/v2/monitor/health
```

Without `AIRFLOW_UID`, Linux log files end up owned by uid 50000 (see Stop
and reset). macOS and Windows need no `.env`.

Healthy means: four services `(healthy)`, `airflow-init` `Exited (0)`, and
the health JSON shows `metadatabase`, `scheduler` and `dag_processor` as
`healthy`. `triggerer` is `null` because none is deployed.

Smoke test:

```bash
docker compose exec airflow-scheduler airflow dags list-import-errors
docker compose exec airflow-scheduler airflow dags unpause hello
docker compose exec airflow-scheduler airflow dags trigger hello
docker compose exec airflow-scheduler airflow dags list-runs hello
```

`list-import-errors` prints `No data found` when every DAG file imports. The
hello run reaches `success` within a minute.

### Run the data pipeline DAG

`Data-Pipeline/dags/daily_pipeline.py` chains ingestion, processing and
validation, one task per module (#22). `docker-compose.yaml` mounts the repo
under `/opt/airflow/repo` (scripts, config and `docs/data` read-only,
`Data-Pipeline/data` writable) and puts `Data-Pipeline/scripts` on
`PYTHONPATH`, so tasks import the scripts (ADR-017).

- Twenty tasks run scripts: the 311 downloads (`ingest_legacy_311` and
  `ingest_new_311`, #6), `ingest_static_layers`, `ingest_alert_history`, the
  five weather and tide downloads (`ingest_noaa_logan`, `ingest_nws_forecasts`,
  `ingest_nws_alerts`, `ingest_tides` and `ingest_open_meteo`, #7 and #8),
  `validate_raw`, `parse_legacy_311`, `parse_new_311`, `combine_311`,
  `spatial_join`, `daily_counts`, `surge_labels`, `feature_table`,
  `validate_processed`, `schema_and_statistics` and `bias_slicing`. `parse_legacy_311` runs
  once per legacy year, at most 4 at a time by default (`LEGACY_PARALLEL`),
  and `merge_legacy_311` joins the years with `legacy_311.read_years`' rule
  for repeated case ids (#37). `check_range` starts the run and `push_data`
  ends it.
- No task is a placeholder now. A task listed in `PLANNED`
  (`Data-Pipeline/scripts/pipeline_steps.py`) logs the issue that builds it and
  succeeds.
- `ingest_legacy_311` and then `ingest_new_311` refresh the raw 311 files in
  `Data-Pipeline/data/raw/311/` on their schedule, for any range (see "311
  ingestion" below).

The DAG runs only when triggered. It takes two params, both inclusive:
`start` (2015-01-01 or later) and `end` (empty: through the latest data).

```bash
docker compose exec airflow-scheduler airflow dags unpause daily_pipeline
# One year, into Data-Pipeline/data/slices/2025-01-01_2025-12-31/
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r slice-2025 -c '{"start": "2025-01-01", "end": "2025-12-31"}'
# Full build, into Data-Pipeline/data/interim/
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r full-build
docker compose exec airflow-scheduler airflow dags list-runs daily_pipeline
docker compose exec airflow-scheduler airflow tasks states-for-dag-run daily_pipeline slice-2025
```

In the UI, Trigger opens a form with the two dates. A slice never overwrites
the full-build files that other scripts read; delete its folder in
`Data-Pipeline/data/slices/` when you are done. A slice keeps only its days,
in Boston local time (new-system times are already local, ADR-020): legacy
rows are cut right after parsing, and the combined table after combining.
The parsed new-system export stays whole, because combining needs every new
case to set the switch dates and to drop resubmitted legacy copies. So a
slice's `311_combined.parquet` holds the same rows as the full build has for
those days. A run fails at `check_range`
on a start before 2015, an end before the start, or an unknown conf key.
Each task's log, with everything its script printed, is in
`Data-Pipeline/logs/dag_id=daily_pipeline/run_id=<run>/task_id=<task>/`.
On a laptop (2026-10-24, the `brt-airflow:local` image, #68 in), the full
build took 73 s with the legacy years side by side, against 124 s one after
another, and peaked at 2.5 GiB in the scheduler container (`docker stats`).
The README's "Pipeline performance" section has the Gantt charts and numbers.
To parse fewer years at once, for example 2 on the VM if #61 shows memory is
tight, put `LEGACY_PARALLEL=2` in `.env` next to `docker-compose.yaml` and run
`docker compose up -d --build --wait`. Without it the DAG runs 4, and
Airflow's parallelism (4) caps any higher value. A value that is not a whole
number of 1 or more stops the DAG from importing; `list-import-errors` shows
why.

Logging and failures (#36):

- Every task logs one line with its run id, rows in and rows out, for example
  `run slice-2025 task combine_311: rows in 348,466, rows out 280,044 in 3.1 s`.
  Placeholders log 0 and 0, and `check_range` logs n/a. CSV inputs are
  counted as records, the way the parsers report `file_rows`
  (`legacy_311.count_records`), and parquet files from their metadata;
  `ingest_static_layers` counts only the layers in its config, and the 311
  ingest tasks count the records in the files they keep. Counting reads
  each raw CSV once more: about 1 s per yearly file in the container on a
  Windows laptop, 12 s for all 13 files.
- The `ingest_*` tasks retry twice. Airflow's exponential backoff adds a
  random spread, so the waits are about 5 to 10 minutes and then 10 to 15
  (capped). Every other task fails at once.
- A real step whose input file is missing or empty fails before its script
  runs (`InputError`), naming the file and how to get it. The DAG raises it
  as `AirflowFailException`, so even an `ingest_*` task fails at once instead
  of retrying. A script error is logged as `run <id> task <task> failed:
  <error>` and keeps its own type.
- Before a step runs, its outputs are deleted, so a file left by an earlier
  run cannot pass for new output. The 311 ingest tasks are the exception:
  their outputs are the downloads, which stay until a changed source replaces
  them. `ingest_static_layers` also deletes empty (0-byte) files in
  `Data-Pipeline/data/raw/static/`, so `--download` fetches them again; the
  311 ingest tasks do the same in `Data-Pipeline/data/raw/311/`.
- A task that fails for good writes `ALERT <dag>.<task> failed in run <id>
  (try <n>): <error>` to its own log (`Data-Pipeline/scripts/alerts.py`), and
  posts the same line to Slack when a webhook is set (below). The callback
  runs once, after the last try, so one failed task is one alert.

### Pipeline alerts (Slack)

Failure alerts go to Slack through an incoming webhook (#34). The URL is the
credential: anyone who has it can post to the channel, so it lives only in the
gitignored `.env` locally, never in chat, a ticket or the repo. On airflow-vm
it comes from Secret Manager with #64. Without it, alerts stay in the task log
and runs are unaffected. Before an alert is logged or posted, any API key from
`.env` (or the webhook) that appears in it, as typed or URL-encoded, is replaced
by its name, for example `key=<CENSUS_API_KEY>`.

Set it up once (the webhook's owner; the team only needs to join the channel):

1. In Slack, create an app for the workspace (api.slack.com/apps, "From
   scratch"), turn on **Incoming Webhooks**, and add a webhook to the alert
   channel. Invite the team to that channel.
2. Copy the webhook URL and put it in `.env` as one line:
   `SLACK_ALERT_WEBHOOK=https://hooks.slack.com/services/...`
3. Recreate the stack so the containers see it: `docker compose up -d --wait`.
4. Send a test alert:

   ```bash
   docker compose exec airflow-scheduler python /opt/airflow/repo/Data-Pipeline/scripts/alerts.py --test
   ```

   It logs `ALERT test alert ...` and `alert sent to Slack`, and the message
   appears in the channel.

To check the whole path, inject a bad file and run a slice: drop a required
column (for example `open_dt`) from `Data-Pipeline/data/raw/311/2025.csv`,
then trigger `daily_pipeline` with `{"start": "2025-01-01", "end":
"2025-01-31"}`. `validate_raw` fails with `ValidationFailed`, the tasks after
it are `upstream_failed`, and the run sends exactly one alert, from
`validate_raw`, starting with `raw 2025.csv header:
expect_column_to_exist(open_dt)`. Restore the file afterwards (re-download, or
`dvc pull` once #18 tracks it).

- **`... is not set (or not a Slack webhook)`.** The variable is empty in the
  container, or does not start with `https://hooks.slack.com/`. Check `.env`,
  then recreate the stack.
- **`Slack refused the alert: HTTP 403` or `404`.** The webhook was revoked or
  mistyped. Create a new one and replace the line in `.env`.
- **`could not send the failure alert: ...` in a task log.** Slack could not
  be reached; the task's own result is unchanged, and the alert is still in
  the log line above it.

### Data validation (Great Expectations)

`validate_raw` runs before parsing and `validate_processed` after the
processing steps (#17). Both run `Data-Pipeline/scripts/validate_data.py`
with the bounds in `Data-Pipeline/config/validation_bounds.csv`; change a
bound there in a reviewed PR, not in the code. Usage analytics are switched
off (`GX_ANALYTICS_ENABLED=False`).

- **Raw:** each legacy yearly file in the run's range, one at a time, and the
  new-system export: the parsers' required columns and coordinates, case ids
  present and unique, open times that parse, no non-zero offset on
  new-system times (`new_311.has_other_offset`, ADR-020), the median open
  hour (catches a switch to real UTC), the row count against
  `published_311_counts.csv` (legacy years and the new-system export), and
  coordinates inside Boston.
- **Processed:** `311_combined.parquet` and `311_located.parquet`: expected
  columns, unique case ids, known `source_system`, `storm_group`, `match`,
  `location` and `unit` values, open times from 2015-01-01 to the run date,
  the median open hour per system, the same row count in both tables, and the
  share of requests placed in every year.

Checks are blocking or warnings. A failed blocking check fails the task, so
nothing after it runs. A warning is printed (`WARNING <table>: <check>`) and
kept in the JSON, and the run goes on, because the parsers already handle
it: an optional column missing (anything in `KEEP_COLUMNS` outside the
parsers' `REQUIRED_COLUMNS` and the coordinates; the parsers fill it with
blanks), or a few missing or repeated case ids (the parsers drop and count
them). More than `case_id_ok_min` (0.1%) of ids missing or repeated blocks.
`placed_per_year_min` leaves out years with fewer than
`placed_min_year_rows` rows, so early January does not fail on a few rows.

A failed blocking check fails the task. The task log ends
with `ValidationFailed: <n> <stage> checks failed: <table>: <check>(<column>)
observed <value>`, and every result is in
`Data-Pipeline/data/interim/validation/raw.json` or `processed.json` (for a
slice, under its folder in `Data-Pipeline/data/slices/`). To check by hand:

```bash
python Data-Pipeline/scripts/validate_data.py --stage raw
python Data-Pipeline/scripts/validate_data.py --stage processed
```

What to do when one fails:

- **`expect_column_to_exist` on a header.** The city renamed or dropped a
  required column (as a warning: an optional one). Check the published file
  before changing any parser.
- **`expect_column_median_to_be_between(open_hour)` or `offset_ok`.** The
  new-system clock may have changed. Rerun `map_311.check_clock` (it runs in
  `combine_311`) and read ADR-020 before changing `new_311.CLOCK`.
- **`expect_table_row_count_to_be_between` on a legacy file.** The download
  is truncated or the city revised the file. Download it again, and update
  `published_311_counts.csv` in a PR only if the city's count changed.
- **`placed_share` or `in_boston`.** Coordinates went missing or were swapped
  for many rows; look at the year in the JSON before rerunning.

- **`ingest_static_layers` fails with `No module named 'geopandas'`.** The
  containers run an image built before the geo packages were added (ADR-022).
  Rebuild with `docker compose up -d --build --wait`.
- **Linux: "Permission denied" in `Data-Pipeline/data`.** The stack ran
  without `AIRFLOW_UID`, so `airflow-init` gave the folder to uid 50000. Set
  `AIRFLOW_UID` (Start and check), then take it back:
  `sudo chown -R "$(id -u):$(id -g)" Data-Pipeline/data`.

### Stop and reset

```bash
docker compose down      # keeps the postgres-db-volume (run history)
docker compose down -v   # deletes it (full reset)
```

The containers restart whenever Docker starts until `docker compose down`.
`down -v` deletes the database volume only; task logs in `Data-Pipeline/logs/`
are host files and stay.

On Linux, if the stack ever ran without `AIRFLOW_UID`, those log files belong
to uid 50000 and deleting them fails with "Permission denied". Take them back:

```bash
sudo chown -R "$(id -u):$(id -g)" Data-Pipeline/logs
```

### Overrides and secrets

`.env` is for local development only. macOS and Windows need none; Linux
needs `AIRFLOW_UID` (above). Other local overrides use the names in
`.env.example`. Postgres and the admin user keep their first password until
`docker compose down -v`.

The VM does not use `.env` for secrets: they come from Secret Manager
(ADR-015), set up in #20. However they are supplied, the JWT secret, API
secret key and Fernet key must be identical in every container; the shared
environment block in `docker-compose.yaml` passes one value to all of them.

### API keys in .env

Pipeline tasks read `NOAA_CDO_TOKEN`, `CENSUS_API_KEY` and `NWS_USER_AGENT`
from the environment (docs/data-access.md). Locally they come from `.env`.
This section is for a laptop; on airflow-vm the keys reach `.env` with #64.
A team member fills them from Secret Manager (needs `gcloud auth login`; the
command names project 1, so another default gcloud project does not matter).
Run it in the repo root. It replaces any existing lines for the three keys,
keeps the rest of `.env`, and makes the file readable only by you (writing
`.env.tmp` and moving it resets the permissions):

```bash
touch .env && grep -vE "^(NOAA_CDO_TOKEN|CENSUS_API_KEY|NWS_USER_AGENT)=" .env > .env.tmp; mv .env.tmp .env
for s in noaa-cdo-token census-api-key nws-user-agent; do
  printf "%s='%s'\n" "$(echo "$s" | tr a-z- A-Z_)" "$(gcloud secrets versions access latest --secret="$s" --project=boston-resilience-twin)" >> .env
done
chmod 600 .env
docker compose up -d --wait
```

The values never print to the terminal. Do not `cat .env` in a shared
screen. To check that the containers see each key without printing it:

```bash
docker compose exec airflow-scheduler sh -c 'for k in NOAA_CDO_TOKEN CENSUS_API_KEY NWS_USER_AGENT; do [ -n "$(printenv $k)" ] && echo "$k set" || echo "$k empty"; done'
```

A secret slot with no version yet (for example `nws-user-agent` before its
value is added) writes an empty value and gcloud prints `NOT_FOUND`; the task
that needs it then fails naming the key. On Linux, write `AIRFLOW_UID` first:
the README's `echo "AIRFLOW_UID=$(id -u)" > .env` overwrites the file.

### Troubleshooting

- **Docker Desktop quits a few seconds after it starts (Windows).** Podman
  Desktop also claims the `\\.\pipe\docker_engine` pipe, and Docker Desktop
  crashes when the pipe is taken. Quit Podman Desktop (system tray), then
  start Docker Desktop. Turn off Podman Desktop's start-on-login to keep it
  from happening after a reboot.
- **Every compose command fails with "unexpected character" in `.env`.** The
  file was written as UTF-16 by Windows PowerShell 5.1 `>` or `Out-File`.
  Recreate it with `Copy-Item .env.example .env` or an editor saving UTF-8.
- **Port 8080 is busy.** Set `AIRFLOW_HOST_PORT` in `.env`.
- **"Permission denied" on `Data-Pipeline/logs` (Linux).** The stack ran
  without `AIRFLOW_UID`. Run the `sudo chown` under Stop and reset, then
  write `.env` before the next start.
- **A DAG does not show up.** Run `airflow dags list-import-errors` (above).
  New files are picked up within 30 seconds. A new component also needs a
  volume line for its `dags/` folder in `docker-compose.yaml`, mounted at
  `/opt/airflow/dags/<component>` in snake_case (ADR-017); without it, Airflow
  never sees the folder.
- **A DAG task fails with `ModuleNotFoundError` after a pull.** The image is
  stale: someone added a package to `Data-Pipeline/requirements.txt` and your
  containers still run the image built before it. Rebuild with
  `docker compose up -d --build --wait`.
- **DAGs still load from the old path after a pull.** A running stack keeps
  its old mounts until its containers are recreated. After pulling a change
  to `docker-compose.yaml`, such as the ADR-017 DAG mount, run
  `docker compose down`, then `docker compose up -d --build --wait`.

## CI

`.github/workflows/ci.yml` runs on every pull request and every push to
main: check Python is 3.12, `ruff check .` (errors only, rules in
`ruff.toml`), install `Data-Pipeline/requirements.txt`, then
`python -m pytest Data-Pipeline/tests`. It uses no secrets, and
`Data-Pipeline/tests/conftest.py` blocks network access in every test, so a
test that calls a live API or Cloud Storage fails with `NetworkBlocked`.
The job is cancelled after 10 minutes; a normal run takes 1 to 2.

Run the same checks locally before pushing:

```bash
ruff check .
python -m pytest Data-Pipeline/tests
```

**Making the check required (repo admin, once).** After the workflow has run
once on a pull request, open Settings, Branches, the rule for `main`, tick
"Require status checks to pass before merging", search for
`Lint and test (Python 3.12)`, add it, and save. If the job name in
`ci.yml` ever changes, update the required check too, or every pull request
waits for a check that never reports.

- **`NetworkBlocked: test tried to look up ...` or `... tried to connect to ...`.**
  The test reaches the internet; a request by host name usually fails at the
  lookup. Connections to `127.0.0.1` and `::1` are allowed. Save a small
  response under the test's fixtures, or patch in a
  fake (see `test_pipeline_steps.py`, which replaces `static_layers.download`),
  instead of calling the API.
- **Lint fails with `F401` (unused import) or `F841` (unused variable).**
  Remove it, or run `ruff check --fix .`.
- **`Check Python is 3.12` fails.** `.python-version` changed; ADR-013 pins 3.12.

## Surge baseline re-check (ADR-005)

ADR-005 builds surge baselines from the same weeks of the previous 3 years,
both 311 systems combined. Rerun the check when any of these happens:

- the first new-system snow of 25 mm or more at Logan;
- a `docs/data/category-map.csv` PR that touches a storm group;
- the live quiet-day surge rate stays above the replay maximum for 30 days.

```bash
python Data-Pipeline/scripts/legacy_311.py
python Data-Pipeline/scripts/new_311.py
python Data-Pipeline/scripts/map_311.py --change-points Data-Pipeline/data/interim/change-points.csv
python Data-Pipeline/scripts/live_baselines.py --change-points Data-Pipeline/data/interim/change-points.csv
```

`map_311.py` writes the switch dates to the gitignored data folder here and
`live_baselines.py` reads them from there, so the committed
`docs/data/change-points.csv` only changes in a reviewed PR.
For snow, compare snow-day counts with the legacy range and look for
unmapped or non-snow categories that jump on snow days; open a category-map
PR if so, and update ADR-005.

## After-switch policy (ADR-021)

`map_311.py` flags legacy requests opened on or after their category's
first new-system day (`after_switch`). ADR-021 keeps them. The setting is
`after_switch_policy` in `Data-Pipeline/config/processing.csv`: `keep`
(default), `drop`, or `exclude_from_baselines` (counted on their day, left
out of baselines). Change it only in a reviewed PR that updates ADR-021,
regenerates counts and labels, and reports the change in totals.

Rerun the check after a new 311 export, after a category-map PR, and at the
first snow of the winter:

```bash
python Data-Pipeline/scripts/legacy_311.py
python Data-Pipeline/scripts/new_311.py
python Data-Pipeline/scripts/map_311.py --change-points Data-Pipeline/data/interim/change-points.csv
python Data-Pipeline/scripts/static_layers.py --download --manifest Data-Pipeline/data/interim/static-layers.csv
python Data-Pipeline/scripts/after_switch.py
```

The `--manifest` path keeps the committed `docs/data/static-layers.csv` as
it is. `after_switch.py` takes about 1.5 minutes and writes no files. Look
for a window where both systems logged many rows and the ratio under keep is
well above 1 (requests entered twice), and in winter for Snow Plowing rows
flagged `after_switch`.

## Weather and tide downloads

Four `daily_pipeline` tasks keep the raw weather and tide history (#7). They
are `ingest_*` tasks, so they retry twice on a network error; a missing key
fails at once, naming it.

| Task | Script | Source | Stored under `Data-Pipeline/data/raw/` | Key |
|---|---|---|---|---|
| `ingest_noaa_logan` | `noaa_logan.py` | NOAA CDO API v2, GHCN-D Logan (USW00014739), daily from 2015 | `weather/noaa_logan/<year>.json` | `NOAA_CDO_TOKEN` |
| `ingest_tides` | `tides.py` | CO-OPS Boston gauge 8443970, hourly observed and predicted from 2015, metres above MLLW, GMT | `tides/observed/` and `tides/predicted/`, `<year>-H1.json` and `-H2.json` | none |
| `ingest_nws_forecasts` | `nws.py forecasts` | NWS gridpoint data, office BOX at City Hall | `weather/nws_forecasts/<UTC date>/<updateTime>.json` | `NWS_USER_AGENT` |
| `ingest_nws_alerts` | `nws.py alerts` | NWS active alerts, zone MAZ015 and county MAC025 | `weather/nws_alerts/<UTC date>/<updated>.json` | `NWS_USER_AGENT` |

Files are stored exactly as the source returned them (NOAA's pages are joined
into one file per year, values unchanged). Reruns are safe:

- **History (NOAA, tides).** A `_manifest.json` records each file's period,
  download time and SHA-256. It lives in `Data-Pipeline/data/state/`
  (`state/weather/noaa_logan/`, `state/tides/observed/` and `/predicted/`),
  not in `raw/`: it changes every run, so a run with nothing new leaves `raw/`
  byte for byte as it was and DVC records no new version. A period is final once it was downloaded
  long enough after it ended for the source to stop revising it: NOAA 30 days,
  tide observations 45 days (verified data lags about a month), predictions at
  once. A final file is never fetched again; any other is fetched whole and
  replaces its file only after its checks pass, so a failed run keeps the old
  file. CO-OPS takes at most 365 days per request, hence the half-years.
- **Snapshots (NWS).** NWS keeps no archive, so each run saves what is current,
  named by NWS's own issue time. A rerun with nothing new writes nothing.

Run one task, or a script directly from the repo root:

```bash
docker compose exec airflow-scheduler airflow tasks test daily_pipeline ingest_tides
python Data-Pipeline/scripts/tides.py
python Data-Pipeline/scripts/noaa_logan.py     # needs NOAA_CDO_TOKEN
python Data-Pipeline/scripts/nws.py forecasts  # needs NWS_USER_AGENT
```

The first tide run fetches 48 half-years (about 30 s); after that a run
fetches only the current half-years.

Logan reports no snow depth (`SNWD`) from 2015 on, although the station has
it in older years, so those files hold seven of the eight elements. Peak gust
(`WSF5`) misses 8 days in 2015 to 2023, the gap ADR-003 counts.

- **`<task> cannot start: NOAA_CDO_TOKEN not set` (or `NWS_USER_AGENT`).** The
  key is empty in the container. Fill `.env` ("API keys in .env"), then
  `docker compose up -d --wait`.
- **NOAA `HTTP 400 ... Token parameter is required` or `not valid`.** The
  token is missing or wrong; check it with docs/data-access.md.
- **NOAA `HTTP 429` or `HTTP 503`.** CDO's rate limit or an outage; the task
  retries, and allows 10,000 requests a day per token.
- **NWS `HTTP 403`.** The user agent is empty or not accepted.
- **`... has no data for a ... long over`.** A finished period came back empty,
  which a source should never do; the run stops instead of storing nothing.
  Check the source's status page and rerun later.
- **`_manifest.json is not valid JSON`.** Fix it, or delete it (under
  `Data-Pipeline/data/state/`): the source is then fetched again from 2015,
  replacing files only where they changed.

## Static layers

`Data-Pipeline/config/static_layers.csv` lists each static layer: file name, source URL, publisher, vintage, license, and the key column that must be present, unique and non-blank. To add or replace a layer, change a row there in a reviewed PR.

```bash
python Data-Pipeline/scripts/static_layers.py --download   # fresh clone: fetch missing files, then load
python Data-Pipeline/scripts/static_layers.py              # files already in Data-Pipeline/data/raw/static/
python Data-Pipeline/scripts/static_layers.py --refresh    # fetch every file again (source updated)
```

Raw files go to `Data-Pipeline/data/raw/static/` and the loaded layers to `Data-Pipeline/data/interim/static/` (vectors as GeoParquet in EPSG:4326), both gitignored. The run rewrites `docs/data/static-layers.csv` with the source CRS, row count, retrieval date and SHA-256 of each file; commit it with the change. A changed hash after `--refresh` means the city published a new version, so check the row counts before using it. The run stops on a missing, empty or unreadable file, a missing or duplicate key, or a layer that falls outside Boston after reprojection (usually a wrong CRS).

## Weather download (Open-Meteo, issue #8)

`Data-Pipeline/scripts/open_meteo.py` downloads hourly Open-Meteo weather at Boston Logan for each set in `Data-Pipeline/config/open_meteo.csv`. What each set is, and which one validation and test may use: the table in [docs/data/README.md](data/README.md#open-meteo-weather). To change a model, start date, value or the place, change the config in a reviewed PR. Set c (day-before forecasts) starts 2024-01-21, GFS's first full day of day-before values, and has no rain or snowfall before early December 2024 (README table).

```bash
python Data-Pipeline/scripts/open_meteo.py --dry-run                              # what a run would download; writes nothing
python Data-Pipeline/scripts/open_meteo.py                                        # download what is missing or newly finished
python Data-Pipeline/scripts/open_meteo.py --only first_hours_ecmwf_ifs --force   # download one set again, every year
```

What a run does, for each set:
1. Finds the last day to store. `end = yesterday`: yesterday in UTC, so no hour after the download is stored. `end = latest_values` (ERA5, about 5 days behind): one small request finds the last full day with values.
2. Splits the dates into calendar years. A year already stored through that day is skipped without a request. For `end = yesterday`, a year counts as stored only once its last day has every hour of every value that has data. A last day with gaps, for example a 31 December downloaded before Open-Meteo had its evening hours, is never taken as complete. It is fetched again on every run for 7 days after that day, then once a week until the gaps are filled. Until then its manifest entry has `final_day_complete: false`, the values with gaps (`final_day_gaps`) and the date of the last check (`final_day_checked`), `docs/data/open-meteo.csv` shows the missing hours, and a run that skips it prints `last day still has gaps in [...], next check on or after <date>`. A check that brings nothing new rewrites no year file. A value with no data at all (showers in ECMWF IFS before 2025) stays missing and is not a gap.
3. Downloads each other year whole and checks the answer: not empty, valid JSON, no Open-Meteo error, in UTC, every hour and value present, numbers only, grid point within 0.5 degrees of Logan. Only then does it replace the year file, and only if its bytes changed. A failed check keeps the stored file, logs the year as `failed`, and the run exits with code 1.
4. Updates the set's `_manifest.json` (download time, start date, last hour stored, grid point, hours with data per value, whether the last day is complete) and adds a line to `_runs.jsonl`, both under `Data-Pipeline/data/state/open_meteo/<set>/`.
5. After a run with no failure, writes `docs/data/open-meteo.csv` with the attribution, coverage, download time and SHA-256 of every file, and only if its content changed. With `--only`, the other sets keep their rows as they are, and so does a set with no files on this machine. A run with a failure leaves the file alone. Commit it with the change; a DAG run should write it under `Data-Pipeline/data/` with `--attribution`, and the `ingest_open_meteo` task does: `Data-Pipeline/data/state/open_meteo/open-meteo.csv` (docs/data is read-only in the containers).

Output: `Data-Pipeline/data/raw/open_meteo/<set>/<year>.json` (the downloads, nothing else) and `Data-Pipeline/data/state/open_meteo/<set>/` (`_manifest.json`, `_runs.jsonl`), gitignored and local only. A rerun with nothing new rewrites no year file and no attribution row. Nothing is uploaded; raw data reaches Cloud Storage only through DVC (#18, #19). No API key is needed.

Rate limits: the free API allows 600 calls a minute and 10,000 a day, and a request counts as one call per 2 weeks of data for up to 10 values. A year of one set is about 26 calls, about 31 for set c, which asks for 12 values. The first full downloads were 13 requests for sets a and b (about 300 calls) and 3 requests for set c (about 85 calls), 5 seconds apart (`--pause`). A daily rerun sends 1 to 4 requests: the ERA5 check and the latest year of each set that has new days, plus one for each year whose incomplete last day is due for a check (step 2). HTTP 429 stops the run at once: the remaining years and sets are listed as not tried and nothing more is requested. A temporary server error (HTTP 5xx) is tried again after 10 and 30 seconds.

Run it after about 01:00 UTC, when the model run that covers yesterday's last hours has been published (4 to 6 hours after it starts). An earlier run is still valid, and the next day's run replaces that year whole.

Moving files from the old layout (once). Before the #8 review, each set's `_manifest.json` and `_runs.jsonl` sat in its raw folder. Move them from the repo root before the next run; the year files stay where they are and nothing is downloaded again:
```
mkdir -p Data-Pipeline/data/state/open_meteo/history_era5 Data-Pipeline/data/state/open_meteo/first_hours_ecmwf_ifs Data-Pipeline/data/state/open_meteo/day_before_ncep_gfs_global
mv -n Data-Pipeline/data/raw/open_meteo/history_era5/_manifest.json Data-Pipeline/data/raw/open_meteo/history_era5/_runs.jsonl Data-Pipeline/data/state/open_meteo/history_era5/
mv -n Data-Pipeline/data/raw/open_meteo/first_hours_ecmwf_ifs/_manifest.json Data-Pipeline/data/raw/open_meteo/first_hours_ecmwf_ifs/_runs.jsonl Data-Pipeline/data/state/open_meteo/first_hours_ecmwf_ifs/
mv -n Data-Pipeline/data/raw/open_meteo/day_before_ncep_gfs_global/_manifest.json Data-Pipeline/data/raw/open_meteo/day_before_ncep_gfs_global/_runs.jsonl Data-Pipeline/data/state/open_meteo/day_before_ncep_gfs_global/
ls -a Data-Pipeline/data/raw/open_meteo/*
```
`mv -n` never overwrites; `ls -a` should show only year files. Until the move, the script reads the old manifests in place, never changes them, and logs a warning. If a run already wrote a manifest under `state/`, the old one in the raw folder is out of date: delete it, and keep the old log as `_runs_before_move.jsonl` next to the new one.

When something goes wrong:
- A year shows `failed`: the reason is in the output and in `_runs.jsonl` under `Data-Pipeline/data/state/open_meteo/<set>/`. The stored file is unchanged; fix the cause and rerun.
- `its files were downloaded with other settings`: the config's model, place or values changed under an existing set name. Give the set a new name (a new folder), or rerun with `--force` to replace its files.
- `start moved from <old> to <new>`: the config's start date moved later under an existing set name. Give the set a new name, or rerun with `--force`, which drops the stored years before the new start (manifest entries and year files) and downloads the rest again. An earlier start just downloads the new years.
- `_manifest.json is not valid JSON`, or `entry '<year>.json' lacks ...` (or another entry problem): the manifest under `Data-Pipeline/data/state/open_meteo/<set>/` is broken. Fix the entry, or delete the entry or the whole manifest; the next run downloads those years again.
- `no full day with values in the 14 days up to ...`: ERA5 is late or the API is down. Try again later.
- HTTP 429: the run stopped at once. Wait an hour and rerun; finished years are not downloaded again.
- `docs/data/open-meteo.csv not written`: the run had a failure (fix it and rerun), or the file lost its attribution columns (restore it from git).

## Spatial join (311 requests to tracts and neighborhoods)

`spatial_join.py` places every request in `311_combined.parquet` by its coordinates, in a 2020 tract and the ADR-004 neighborhood that holds it, and writes `Data-Pipeline/data/interim/311_located.parquet`. It needs the static layers loaded first.

```bash
python Data-Pipeline/scripts/spatial_join.py
```

The run adds `geoid20`, `unit` (the neighborhood a request is counted in) and `location`, and prints the share with no unit by year and system. `location` is `placed` or the reason there is no unit: `no_coordinates` (missing or not a number), `placeholder` (a default point listed in `Data-Pipeline/config/placeholder_locations.csv`, matched within about a metre), `outside_boston` (no 2020 tract) or `no_neighborhood` (tract 9815.02, which the city leaves out of every neighborhood). A latitude and longitude entered the wrong way round are swapped back and counted. If a new default point turns up (one spot holding thousands of requests with no neighborhood), add it to the placeholder file in a reviewed PR.

## Daily counts

`daily_counts.py` turns `311_located.parquet` into the table labels are built from (ADR-004): one row per unit, day and storm group, every day present and zero-filled, in `Data-Pipeline/data/processed/daily_counts.parquet`.

```bash
python Data-Pipeline/scripts/daily_counts.py                                     # 2015-01-01 to the last full day of both systems
python Data-Pipeline/scripts/daily_counts.py --start 2025-01-01 --end 2025-12-31 # one year
```

`requests` counts every request kept under `after_switch_policy` (ADR-021); `baseline_requests` counts the ones baselines may use, which under `keep` is the same number. The run prints each step from the combined table to the counted total (unmapped, not storm-related, after_switch, each no-location reason, no date, outside the range) and stops if the counts do not add up to the total.

## Surge labels and features (#16)

Three scripts, each a `daily_pipeline` task (`ingest_alert_history`, `surge_labels`, `feature_table`):

```bash
python Data-Pipeline/scripts/alert_history.py --download   # NWS alert history (ADR-014 A+): pull every year not yet final, then prepare
python Data-Pipeline/scripts/surge_labels.py --grid        # labels under the chosen rule, plus the surge rate of every rule
python Data-Pipeline/scripts/feature_table.py              # features per unit and day, without weather and tides
python Data-Pipeline/scripts/feature_table.py --noaa-dir Data-Pipeline/data/raw/weather/noaa_logan \
  --forecast-dir Data-Pipeline/data/raw/open_meteo/day_before_ncep_gfs_global \
  --tides-dir Data-Pipeline/data/raw/tides/predicted       # with weather and tides, as the feature_table task runs it
```

**Alert history.** `alert_history.py` pulls the Iowa Environmental Mesonet bulk VTEC download for NWS office BOX one year at a time into `Data-Pipeline/data/raw/nws_alerts/box_<year>.csv`, as published. IEM files an alert under the year it starts, so from 15 December the next year is pulled too. A year is final once it was pulled at least 14 days after it ended, because alerts that start late in December are still cancelled or upgraded in January; until then every `--download` pulls it again, and `--refresh` pulls every year. `_downloads.json` in `Data-Pipeline/data/state/nws_alerts/` (`--state-dir`) records when each year was pulled, so `raw/nws_alerts/` holds the IEM files and nothing else, as with #7 and #8; a pull that brings the same bytes leaves the stored file as it was. The first `--download` after the A+ change pulls every year once. A pull without IEM's columns (an error page) never replaces a stored year. It writes `Data-Pipeline/data/interim/nws_alerts.parquet`: one row per area and first bulletin for zone MAZ015 and county MAC025, with the first bulletin (`utc_prodissue`), the first-announced end (`utc_init_expire`) and `ended_early_utc`, the time IEM records a cancellation or upgrade (status CAN or UPG with `utc_expire` equal to `utc_updated`). The other last-state fields are dropped, so no feature can use them (ADR-014, Option A+). The run prints alerts per year and area, to compare with ADR-014, how many alerts ended early, a warning for any CAN or UPG row that fails the check, and any alert type missing from `docs/data/alert-map.csv`; add a row for it in a reviewed PR.

**Evening freshness check (pending #39).** In the evening run, after 18:00 and after #7's `ingest_nws_alerts` has saved a snapshot, `alert_history.py --download --check-nws Data-Pipeline/data/raw/weather/nws_alerts --settings Data-Pipeline/config/labels_features.csv` stops with `FreshnessError` before writing when the IEM pull or the newest snapshot is older than the cutoff, or when a Boston alert message the NWS sent before 18:00 (or its cancellation or upgrade) is not in the pull yet. The task retries on its own; if it keeps failing, check https://mesonet.agron.iastate.edu and run the task again later. Never build the features without the check: a missing alert would count as zero. It logs how many minutes IEM's newest update is older than the pull, the most IEM can have lagged. The daily DAG does not run the check yet: the evening schedule is #39 and the snapshots come from #7.

**Labels.** `surge_labels.py` compares each storm group's requests with its ADR-005 baseline (built from `baseline_requests`) under the rule marked `chosen` in `Data-Pipeline/config/surge_rules.csv`. A cell is a surge when any group in `label_groups` (`Data-Pipeline/config/labels_features.csv`) surges (ADR-004). With fewer than 20 history days there is no label: `surge` is blank, not false, so labels start on 2016-01-06. To change the rule, mark another row `chosen` in a reviewed PR; the rule is frozen by ADR before Model Development.

**Features.** `feature_table.py` writes `Data-Pipeline/data/processed/features.parquet`, one row per unit and day: 311 lags up to day D minus `feature_min_lag_days`; NWS alerts first sent before `forecast_cutoff_local` (18:00 Boston time) the evening before D whose first-announced end is after D begins and that were not cancelled or upgraded before that cutoff, one count per alert even when it covers both zone and county; calendar fields; static attributes from `Data-Pipeline/data/interim/static/`. With `--noaa-dir` and `--forecast-dir` it adds weather: Logan's observed values of day D up to 2023-12-31, and after that the GFS day-before forecasts known at the cutoff (ADR-003), with `wx_source` saying which. With `--tides-dir` it adds the highest predicted water level of the Boston day. Missing weather and tide values stay blank, and the run prints how many forecast days are missing. If this year's NOAA or GFS file or this half-year's tide file is not stored yet, its days stay blank and the task log has a warning naming the file and the features left blank; a missing earlier file stops the task. It stops if a row uses a later 311 day, a 311 feature differs from the counts read by date, a cutoff is not before D, a row after 2023 has observed weather of its own day or a forecast available only after the cutoff, or a day has no weather or tide row at all. `Data-Pipeline/config/feature_catalog.csv` lists every column and whether the model may use it. The `feature_table` task passes the three folders the ingest tasks fill and stops before it runs, naming the folder, if one is missing (for example when `ingest_noaa_logan` has never run because `NOAA_CDO_TOKEN` is not set). A GFS value counts as available 7 hours after its run started (`forecast_issue_margin_hours`), so day D takes 1-day values only from runs up to 12 UTC on D-1 and 2-day values for its later hours; the evidence is in `docs/feature-table.md`.

**Slices.** A label needs the 3 years before its day and a feature the 28 days before, so in a slice both tasks read the full build's `Data-Pipeline/data/processed/daily_counts.parquet` and write only the slice's days. Run the full build once before a slice. If its counts end before the slice does, both tasks stop before writing and name the missing days (`daily counts cover ... have no counts`); run the full build again, then the slice.

## Bias slicing (#32)

`bias_slicing.py` slices the labels and the located requests by neighborhood, ACS group, flood zone and 311 system, and tests each gap. It is the `bias_slicing` task, after `validate_processed`.

```bash
python Data-Pipeline/scripts/bias_slicing.py   # about 40 s on the full build
```

- **Reads** `Data-Pipeline/data/processed/labels.parquet` (`surge_labels`), `Data-Pipeline/data/interim/311_located.parquet` (`spatial_join`) and the static layers in `Data-Pipeline/data/interim/static/`; thresholds in `Data-Pipeline/config/bias_slicing.csv`.
- **Writes** `slices.csv` (every rate, with numerator and denominator), `gaps.csv` (every test) and `report.md` to `Data-Pipeline/data/interim/reports/bias/` (a slice's own `interim/reports/bias/` for a slice run). It prints how many gaps are significant; a gap never fails the task.
- **Reviewed copy.** After a full build whose results go to review, copy the three files to `Data-Pipeline/reports/bias/` in the PR, so the committed report matches the data it was run on, and note the data date in `docs/status.md`.
- **A significant gap** fails the four-fifths rule (`ratio_min`) and its test gives p under `alpha`. Change either only in a reviewed PR; #33 decides what to do about each gap.
- **Run only this task in Airflow** with `docker compose exec airflow-scheduler airflow tasks test daily_pipeline bias_slicing`, after `docker compose exec airflow-scheduler airflow dags pause daily_pipeline`. The test creates a temporary run, and while the DAG is unpaused the scheduler starts that run's other tasks too, downloads and `ingest_static_layers` included, which clears `interim/static/` while the test reads it.
- **Missing locations** count under the neighborhoods their recorded 311 neighborhood name covers, learned from the placed requests with the same name (`name_unit_min_share`, `name_min_placed`); the report lists the names. Most requests without coordinates have no usable name, so these shares are lower bounds.

## Versioned data (DVC)

The DVC remote is `gs://boston-resilience-twin-data/dvc` (`.dvc/config`), a
folder of its own in the data bucket, apart from the raw pulls under `raw/`.
Every team member and `airflow-vm-sa` hold `roles/storage.objectAdmin` on the
bucket (ADR-015), so all of them can pull and push. Storage is about $0.02
per GB a month; noncurrent versions are deleted after 30 days (#48).

DVC reads Google Application Default Credentials, not the `gcloud` login.
On a laptop, log in once:

```bash
gcloud auth application-default login
```

On `airflow-vm` the attached service account is picked up with no login.

Check the remote from the repo root:

```bash
dvc remote list     # gcs  gs://boston-resilience-twin-data/dvc  (default)
dvc status -c       # compares the local cache with the remote; fails without access
dvc pull            # fetch the data the current commit points to
```

Push through the helper, not bare `dvc push`, in anything automated:

```bash
python Data-Pipeline/scripts/dvc_push.py
```

It skips the push, logs one line and exits 0 when there is no access:
`dvc push skipped: no Google credentials ...` when no credentials are found,
or `dvc push skipped: these credentials have no access ...` when the bucket
refuses them (a grader's own Google account), followed by DVC's own error
line. Any other failure, such as a full disk, a network error or a push that
runs past 30 minutes, fails the run.

**In Airflow** the last task of `daily_pipeline`, `push_data`, runs the same
helper. DVC refuses to run outside a git repository, so `docker-compose.yaml`
mounts `.git` (read-only), `.dvc` and `.dvcignore` at `/opt/airflow/repo`.
After pulling a change to those mounts, run `docker compose down`, then
`docker compose up -d --build --wait`. DVC is in the Airflow image (ADR-022),
so a change to its pins in `Data-Pipeline/requirements.txt` also needs that
rebuild. To run only the push task:

```bash
docker compose exec airflow-scheduler airflow tasks test daily_pipeline push_data
```

Inside the containers there are no Google credentials on a laptop, so it logs
`dvc push skipped: no Google credentials ...` and succeeds. On `airflow-vm`
the service account is picked up. To try a real push from a laptop, pass your
login into a one-off container:

```bash
docker compose run --rm \
  -v ~/.config/gcloud/application_default_credentials.json:/tmp/adc.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/tmp/adc.json \
  airflow-scheduler airflow tasks test daily_pipeline push_data
```

- **`push_data` fails with `/opt/airflow/repo is not a git repository`.** The
  containers were started before the DVC mounts. Run `docker compose down`,
  then `docker compose up -d --build --wait`.
- **`dvc pull` fails with `Anonymous caller does not have storage.objects.get`.**
  No Application Default Credentials. Run the login above.
- **`does not have storage.objects.get` with your email.** Your account lacks
  bucket access. Ask the project 1 owner to check `team_members` in tfvars.
- **`UserWarning: ... without a quota project`** or the login's warning that it
  cannot add `boston-resilience-twin` as the quota project. Harmless: Cloud
  Storage does not need a quota project.
- **`dvc: command not found`, or `No module named dvc` from `dvc_push.py`.** Run
  `pip install -r Data-Pipeline/requirements.txt` in the Python 3.12 venv. In
  Airflow, rebuild the image (above).

## Schema and statistics (every run)

`schema_stats.py` describes the processed tables on every run (#35): for each table, the schema (columns, types, share missing) and summary statistics (numeric spread, category shares, rows per year). In `daily_counts`, where every unit and group has the same number of rows, the category shares are shares of requests, so a storm group's share of the counts is tracked.

```bash
python Data-Pipeline/scripts/schema_stats.py            # located and daily_counts, into Data-Pipeline/data/processed/stats/
python Data-Pipeline/scripts/schema_stats.py --no-docs  # skip the Great Expectations data docs
```

Outputs in `Data-Pipeline/data/processed/stats/` (versioned with the rest of `Data-Pipeline/data/` by DVC):

- `<table>.json`: the schema and statistics. Replaced each run, so the previous run's numbers are the version before it.
- `drift.json`: shifts from the previous run beyond the thresholds in `Data-Pipeline/config/stats_thresholds.csv`, plus every schema change (a column added, removed or retyped).
- `data_docs/index.html`: Great Expectations data docs showing the same numbers. Open it in a browser.

The run compares with the previous run's files before replacing them, and builds the data docs before it writes any JSON, so a failed run leaves the previous files as they were. If the statistics, the thresholds and the data docs are all unchanged it writes nothing, so a rerun on the same data leaves the folder as it was. `drift.json` keeps the statistics it compared against (`baselines`), so after a threshold change a rerun on the same data checks that same comparison again with the new values. Shifts are printed and saved but do not fail the run; the validation steps (#17) are what stop a run. To change a threshold, edit the config in a reviewed PR. A table that does not exist yet (`daily_counts` before #15 merges) is skipped and reported.

## Project 1 infrastructure (Terraform)

Config lives in `Deployment/terraform/`. It covers project 1 only (ADR-009):
the Airflow VM, the data bucket, the VM service account, two empty secret
slots, the project 1 APIs, and IAM. Nothing for Cloud Run, Artifact Registry,
Vertex, or Gemini.

State is in the GCS bucket `boston-resilience-twin-tfstate` under the prefix
`project1`. That bucket is not managed by Terraform.

The data bucket `boston-resilience-twin-data` has object versioning on, so an
overwritten raw pull is kept as a noncurrent version. A lifecycle rule deletes
noncurrent versions 30 days after they become noncurrent, which bounds the cost
of keeping every overwrite. Live objects are never deleted by the rule, so a
mistaken overwrite can still be rolled back inside that 30-day window. Confirm
the rule on the bucket with:

```bash
gcloud storage buckets describe gs://boston-resilience-twin-data \
  --format="default(lifecycle_config)"
```

### Cost before you apply

| Resource | Running | Stopped or idle |
|---|---|---|
| `airflow-vm` (e2-medium, us-east1) | about $24.46 a month | $0 of compute |
| 30 GB pd-balanced boot disk | about $3 a month | about $3 a month, billed even when the VM is stopped |
| Ephemeral external IPv4 | about $3.65 a month ($0.005/hour) | $0, released when the VM stops |
| `boston-resilience-twin-data` bucket | storage charged per GB stored | same |
| Service account, secret slots, IAM, API enablement | $0 | $0 |
| **Total** | **about $31.11 a month** | **about $3 a month** |

Budget is $50 of credits per member and is not pooled. The VM stays stopped
when idle in October and runs 24/7 from Nov 1.

### Apply

One time, if not already done:

```bash
gcloud auth application-default login
gcloud config set project boston-resilience-twin
```

Then:

```bash
cd Deployment/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in the real addresses
terraform init
terraform plan -var-file=terraform.tfvars -out=tfplan
terraform apply tfplan
```

`terraform.tfvars` and `tfplan` are gitignored. Never commit either.

Run `terraform plan` once more after the apply. A clean plan ("No changes")
is the check that the config matches what is actually in the project.

### Adding a secret value

Terraform creates the secret slots empty. Values are added by hand and never
go into a `.tf` file, a tfvars file, chat, the repo, or a ticket.

Who can: the members listed in the `secret_writers` tfvars variable. They hold
`roles/secretmanager.secretVersionAdder` on each slot in `local.secret_ids`
(`Deployment/terraform/secrets.tf`) only, which is the permission to add a
new version. It is not
project wide and it does not let them read the values back; reading is
`roles/secretmanager.secretAccessor`, which every team member already has.

```bash
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets versions add <secret> --data-file=-; unset VALUE
```

Run it, paste the value at the blank prompt, and press Enter. `read -s` hides
the paste, and `printf '%s'` sends it without a trailing newline. gcloud stores
stdin byte for byte, so a trailing newline would become part of the key and the
NOAA and Census APIs would reject it. The value never appears in shell history,
and `unset` clears it from the shell even if the gcloud call fails.

To confirm a stored value has no trailing newline (prints its last character):

```bash
gcloud secrets versions access latest --secret=<secret> | tail -c 1 | od -An -c
```

If it prints `\n`, add a new version with the command above and disable the
old one. If a value is ever exposed, add a new version with a fresh value and
disable the old version.

### Stop the VM when idle

```bash
gcloud compute instances stop airflow-vm --zone=us-east1-b
```

Start it again with:

```bash
gcloud compute instances start airflow-vm --zone=us-east1-b
```

The external IP is ephemeral, so the VM gets a different address after every
start. Look it up with:

```bash
gcloud compute instances describe airflow-vm --zone=us-east1-b \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

SSH goes through OS Login:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b
```

Two IAM facts make that work, both of them in Terraform: every team member has
`roles/compute.osLogin`, and every team member has
`roles/iam.serviceAccountUser` on `airflow-vm-sa` alone. The second one is
needed because the VM has a service account attached, and logging in to such a
VM counts as acting as that service account. The grant is scoped to the one
service account, not the project.

A grant to a member who has never signed in to GCP fails with
"User ... does not exist". Each member signs in once, then `terraform apply` is
re-run to pick up the grants that failed.

A stopped VM still plans clean: the config ignores the attributes that move on
their own (the ephemeral IP, the resolved boot image, OS Login SSH keys).

### Sudo on the VM

`roles/compute.osLogin`, which every team member has, is SSH without sudo.
Sudo is `roles/compute.osAdminLogin`, and it is held only by the members
listed in the `vm_admins` tfvars variable (the owner and Srikar). The grant is
on the `airflow-vm` instance, not on the project, so it does not follow to any
other VM that project 1 gains later.

Check whether you have it by running any root command over SSH:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b --command='sudo -n true && echo sudo ok'
```

To give someone sudo, add their `user:` address to `vm_admins` in
`terraform.tfvars` (one line) and apply:

```bash
cd Deployment/terraform
terraform plan -var-file=terraform.tfvars -out=tfplan
terraform apply tfplan
```

Removing the line and applying again takes it away. A member who has never
signed in to GCP fails the same way as the other IAM grants above.

### Tear down

The data bucket has `force_destroy = false`, so a destroy fails while objects
are still in it. Empty it first, deliberately, only when the data is no longer
needed. The 30-day lifecycle rule clears old versions, not live ones, so this
step is still required:

```bash
gcloud storage rm -r --all-versions gs://boston-resilience-twin-data/**
```

Then destroy all Project 1 resources:

```bash
cd Deployment/terraform
terraform destroy -var-file=terraform.tfvars
```

**The state bucket is not destroyed by Terraform.** `boston-resilience-twin-tfstate`
is outside this config on purpose, so a destroy cannot delete the state it is
reading. Remove it by hand, last, after the destroy has finished:

```bash
gcloud storage rm -r --all-versions gs://boston-resilience-twin-tfstate
```

## Airflow on the VM

How Airflow runs on `airflow-vm`, and why: ADR-016. The VM runs the same `docker-compose.yaml` as the local stack, checked out from `main` at `/opt/boston-resilience-twin` and owned by root. Run every command on the VM with `sudo` unless it says laptop. Starting, stopping, and SSH are under "Stop the VM when idle" above. Nobody joins the `docker` group: it is root-equivalent, and `sudo` keeps the operator list equal to ADR-018's.

### First-time setup (new or rebuilt VM only)

Done on 2026-10-18 on the current VM. Docker is not in Terraform yet, so a rebuilt VM needs these steps again.

Install Docker Engine and the compose plugin from Docker's apt repo:

```bash
sudo bash -euo pipefail <<'EOF'
apt-get update -q
apt-get install -y -q ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl is-enabled docker
EOF
```

If apt reports `Could not get lock`, automatic updates are still running from boot. Wait two minutes and rerun; every line is safe to repeat.

Clone `main` and write the VM's `.env`:

```bash
sudo bash -euo pipefail <<'EOF'
git clone -q https://github.com/PritKalariya/boston-resilience-twin.git /opt/boston-resilience-twin
cd /opt/boston-resilience-twin
printf 'AIRFLOW_UID=50000\n' > .env
chmod 600 .env
EOF
```

`AIRFLOW_UID=50000` is deliberate. The compose file's comment says to use `id -u` on Linux, but under `sudo` that returns 0, which would run Airflow as root.

Start the stack:

```bash
cd /opt/boston-resilience-twin && sudo docker compose up -d --build --wait
```

The first start takes about 10 minutes on an e2-medium, half of it pulling the Airflow image. It is not hung.

### After a VM start

Nothing to do. The services use `restart: unless-stopped`, so Docker brings them back at boot, and the stack is healthy about 1 minute after the VM starts. Check with:

```bash
cd /opt/boston-resilience-twin && sudo docker compose ps
```

All four services (postgres, airflow-apiserver, airflow-scheduler, airflow-dag-processor) should show `(healthy)`.

**Stop the VM, never the containers.** Running `docker compose stop` or `down` before a VM stop marks the containers as stopped by hand, and they stay down after the next start. If that happens, run `sudo docker compose up -d --build --wait`.

### Open the UI (laptop)

The API server listens on the VM's `127.0.0.1:8080` only, and no firewall rule opens 8080. Open a tunnel and leave its terminal running:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b -- -N -L 18080:localhost:8080
```

Then browse to `http://localhost:18080` and log in with the compose defaults, `airflow` / `airflow` (ADR-016 explains why that is acceptable). Port 18080 avoids your local stack's 8080. Ctrl+C closes the tunnel. Health without a browser: `curl -s http://localhost:18080/api/v2/monitor/health`.

### Update after a merge to main

```bash
cd /opt/boston-resilience-twin && sudo git pull --ff-only && sudo docker compose up -d --build --wait
```

The VM runs `main` only. Test branches on your laptop.

### Run a DAG from the CLI

```bash
cd /opt/boston-resilience-twin
sudo docker compose exec airflow-scheduler airflow dags unpause <dag_id>
sudo docker compose exec airflow-scheduler airflow dags trigger <dag_id>
sudo docker compose exec airflow-scheduler airflow dags list-runs <dag_id>
```

New DAGs start paused (`DAGS_ARE_PAUSED_AT_CREATION`), and a run triggered on a paused DAG stays `queued`.

### Troubleshooting

- **Triggerer shows red in the UI's health panel.** Expected: the stack has no triggerer service (ADR-016).
- **SSH stalls after "Server accepts key".** Your laptop's ssh-agent has the key but it is locked. Run `ssh-add ~/.ssh/google_compute_engine` on the laptop, then retry.
- **A task is OOM-killed or the scheduler restarts.** With the stack idle, the VM has about 2.5 GB free and no swap. Check `sudo docker inspect -f '{{.Name}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' $(sudo docker compose ps -aq)` and `free -m`.
- **`gcloud compute ssh` offers to create an SSH key.** You ran it on the VM itself. The prompt shows where you are: `@airflow-vm` is the VM.

## 311 ingestion (issue #6)

Code: `Data-Pipeline/scripts/ingest_311.py`. Schedule: `Data-Pipeline/config/ingest_311_sources.csv`. In Airflow it is two tasks of `daily_pipeline` (Run the data pipeline DAG above): `ingest_legacy_311` runs the script with `--system legacy`, then `ingest_new_311` with `--system new_system`. They run one after the other because both write the same schedule state.

### What a run does
For each source, in order:
1. Not due: skip, no request.
2. Due: read the Analyze Boston catalog entry. If change detection is `catalog` and the hash, size, and last-modified time are unchanged, skip.
3. Otherwise download to a temporary file, check it (not empty, the columns its parser reads, at least one record), and only then replace the current file. A download identical to the current file leaves the file untouched. A failed check keeps the current file, logs the source as `failed`, and leaves it due for the next run.

| Files | Every | Change detection |
|---|---|---|
| 2015.csv to 2024.csv | 30 days | catalog |
| 2025.csv | 7 days | catalog |
| 2026.csv | 1 day | catalog |
| new_system.csv | 1 day | none (catalog size is stale), always downloaded when due |

The script writes these local folders only (all gitignored) and never uploads anything; raw data reaches Cloud Storage only through DVC (see Versioning to Cloud Storage below).
- `Data-Pipeline/data/raw/311/`: the downloads only, byte for byte: `<year>.csv` and `new_system.csv`. `legacy_311.py` and `new_311.py` read these. A run with nothing new leaves the folder exactly as it was.
- `Data-Pipeline/data/state/311/_manifest.json`: schedule state, catalog fingerprints, sha256, row counts. A manifest written before the direct upload was removed may still hold an `uploaded` section; it is ignored.
- `Data-Pipeline/data/state/311/_runs.jsonl`: one line per run: every file's action (`downloaded`, `updated`, `skipped`, `failed`), reason, rows, previous rows, malformed rows.
- `Data-Pipeline/data/interim/new_system_as_published.parquet`: new-system records as text, timestamps untouched (they end in `+00` but are Boston local time; see ADR-020), plus `source_system = new`, the label `map_311.py` uses. Written when `new_system.csv` changed or the parquet is missing or is not the one last written. In the DAG it goes to the run's folder, so a slice writes its own.

The first run downloads everything: about 1.7 GB of legacy files plus about 25 MB for the new system.

### Run by hand
From the repo root:
```
python Data-Pipeline/scripts/ingest_311.py --dry-run
python Data-Pipeline/scripts/ingest_311.py
python Data-Pipeline/scripts/ingest_311.py --system legacy
python Data-Pipeline/scripts/ingest_311.py --only legacy_2024 --force
```
`--dry-run` asks the catalog about the due sources and writes nothing. `--system` limits a run to one system, as the DAG tasks do. `--force` ignores the schedule for the selected sources. Exit code 1 means at least one file failed; the current files were kept.

### Move files from the old layout (once)
Before the #6 review the script kept its state and the parquet inside `raw/311/`. If your `Data-Pipeline/data/raw/311/` has `_manifest.json`, `_runs.jsonl` or a `prepared/` folder, run these from the repo root before the first run of this version:
```
mkdir -p Data-Pipeline/data/state/311
mv -n Data-Pipeline/data/raw/311/_manifest.json Data-Pipeline/data/state/311/
mv -n Data-Pipeline/data/raw/311/_runs.jsonl Data-Pipeline/data/state/311/
rm Data-Pipeline/data/raw/311/prepared/new_system.parquet
rmdir Data-Pipeline/data/raw/311/prepared
ls -a Data-Pipeline/data/raw/311 Data-Pipeline/data/state/311
```
- The downloads stay where they are, and the moved manifest keeps their schedule, so nothing is downloaded again.
- `mv -n` never overwrites a file that is already there. `rmdir` removes only an empty folder.
- The old `prepared/new_system.parquet` is derived data with the old label (`new_system`). The next run writes `Data-Pipeline/data/interim/new_system_as_published.parquet` from `new_system.csv`, with `new`.
- `ls -a` should show only the CSV files in `raw/311/`, and `_manifest.json` and `_runs.jsonl` in `state/311/`.
- Until you move them, the script reads the old `raw/311/_manifest.json` without changing it and logs a warning, so a run still downloads only what is due. That run writes `state/311/_manifest.json`, so `mv -n` then leaves the old files in place: delete the old `raw/311/_manifest.json` (it is out of date) and keep the old log next to the new one with `mv -n Data-Pipeline/data/raw/311/_runs.jsonl Data-Pipeline/data/state/311/_runs_before_move.jsonl`.

### Versioning to Cloud Storage
There is no direct upload: issue #6 requires raw data to reach GCS only through DVC. DVC is not set up in the repo yet (no `.dvc/`, no remote, empty `dvc.yaml`; see Blocked in `docs/status.md`), so for now the files stay on the machine that ran the DAG. `raw/311/` holds only the downloads and changes only when a source changed, so DVC sees a new version only then. The `dvc add` and `dvc push` steps for this folder, and who runs them, come with the DVC setup.

`gs://boston-resilience-twin-data/raw/311/` still holds 1,739,455,041 bytes from an earlier direct upload by this script, outside DVC. The team decides whether to keep or delete them; do not delete them before that.

### Airflow environment
The tasks run like every `daily_pipeline` task: `docker-compose.yaml` mounts `Data-Pipeline/data` writable at `/opt/airflow/repo/Data-Pipeline/data`, so a task writes the same folders as a run by hand, and `pipeline_steps.py` passes every path. Like every `ingest_*` task they retry twice (about 5 to 10, then 10 to 15 minutes). A failed source makes the script exit with code 1, which fails the task; a missing `ingest_311_sources.csv` fails at once without retrying. `max_active_runs` is 1 and the two tasks run one after the other, so two runs never write the same files. Unlike other steps, the 311 ingest tasks do not delete their outputs before they run (`keep_outputs` in `pipeline_steps.py`): their outputs are the downloads.

### When something goes wrong
- A source shows `failed`: read its `reason` in the task log or `Data-Pipeline/data/state/311/_runs.jsonl`. The old file is still in place. Fix the cause, then clear the task or wait for the next run.
- `_manifest.json is not valid JSON`: fix it by hand, or delete it. Deleting makes the next run download every file again; files whose content did not change are kept as they are.
- The log says `no schedule state in ... yet; reading .../raw/311/_manifest.json`: the state is still in the old place. See Move files from the old layout.
- 403 `SignatureDoesNotMatch` from S3: the redirect handler in `ingest_311.py` strips the `:443` that Analyze Boston writes into its S3 links. If this comes back, check that `CkanClient` still uses `StripDefaultPortRedirect`.

### Each January
Add a row for the new year (for example `legacy_2027`) with its resource id from `https://data.boston.gov/api/3/action/package_show?id=311-service-requests`, `refresh_days` 1. Change the previous year to 7 and the one before it to 30. Change it by PR, like any config.
