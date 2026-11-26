# ADR-039: The evening run on airflow-vm: schedule, live switch, manifest, scoring, forecast file and checks

- Status: accepted
- Date: 2026-11-25
- Owner: Pu1ni2
- Phase: deploy
- Builds on ADR-029 and ADR-034 (their dated notes of 2026-11-25: the code-sync timer and the `BRT_EVENING_RUN` line, #162 part 1), ADR-031 (`forecast_rows` joins the `heavy_memory` pool), ADR-016 (no triggerer), and the contracts of #159 (`docs/contracts/`).

## Context

The project forecasts, each evening, whether each of the 24 neighborhoods gets a surge of storm-related 311 requests the next day. Until #162, `daily_pipeline` ran only by hand, and nothing called the model service. #162 makes it the live evening run on airflow-vm, from the first live evening (Nov 25) through the Expo (Dec 8).

The pieces it joins were decided elsewhere:

- **#160 (D6)** gives every task of a run one as-of date: the Boston date of the run's `run_after`, or the `as_of` param. A run that starts at or after that date's 18:00 forecast cutoff is an evening run. On an evening run, `forecast_rows` writes tomorrow's 24 rows to `Data-Pipeline/data/forecast/<as_of>/features.parquet`.
- **#161 (D7)** serves the model as `brt-model` on Cloud Run in project 2. Only `airflow-vm-sa` may invoke it (#161; ADR-038 in its PR). It scales to zero, so the first request of an evening may meet a cold start.
- **The contracts (#159)** fix the formats:
  - the manifest whose sha256 is the live `data_version` (`docs/contracts/data-manifest.md`);
  - the `/predict` request and response (`predict.md`);
  - the forecast file (`forecast-file.md`).
- **The data bucket** `gs://boston-resilience-twin-data` (project 1) holds the DVC remote under `dvc/`, and now `manifests/` and `forecasts/`. `airflow-vm-sa` already holds `roles/storage.objectAdmin` on it.

What #162 has to settle:
- when the run starts;
- where the evening tasks run and where they must not;
- what happens when the bucket or the service is out of reach;
- how a missing forecast is noticed;
- what the team sees each evening.

## Decision

1. **Schedule.** `daily_pipeline` runs at 18:05 America/New_York on `CronTriggerTimetable("5 18 * * *", timezone="America/New_York")`, with `catchup=False` and `max_active_runs=1`. That is 23:05 UTC in winter and 22:05 UTC in summer.
   - A scheduled run takes no conf, so it is a full build, the same run ADR-029 relies on to catch up the days the VM was stopped.
   - It starts after the 18:00 cutoff, so it is an evening run with #160's as-of date.

2. **The live switch.** The evening tasks run only where `BRT_EVENING_RUN=1`:
   - That is airflow-vm only. Its startup script renders the line into `.env` (ADR-034, note of 2026-11-25), and `docker-compose.yaml` passes it into the Airflow containers.
   - Anywhere else (a laptop, CI, a grader), each evening task skips with a log line, and `push_data` keeps skipping without bucket access, so `daily_pipeline` still runs with no cloud at all.

3. **The tasks and their order.**
   - `forecast_rows` (#160) runs before `push_data`, in the `heavy_memory` pool (ADR-031), so it never overlaps `schema_and_statistics` or `bias_slicing`.
   - After `push_data` come `write_manifest`, then `score_forecast`, then `evening_summary`.
   - Each new task skips unless the run is an evening run and live.
   - The manifest comes first because every `/predict` request carries its `data_version`.

4. **A lost bucket fails the run.**
   - **The push.** On a live run, `push_data` runs `dvc_push.py --require-access`. It probes the bucket's object permissions first, because a DAG run adds nothing to the DVC cache, so its push may upload nothing and never touch the bucket. No credentials, a missing permission, or DVC's no-access error then fail the task instead of logging a skip.
   - **The manifest and scoring tasks** reach the bucket through `data_bucket.py`, which fails on any refusal.
   - A failed task alerts Slack through the existing `on_task_failure`.

5. **The manifest**, as `docs/contracts/data-manifest.md` defines it:
   - **The files.** The three files (labels, features, tomorrow's rows) are uploaded to their content paths in the DVC remote, `dvc/files/md5/<2 hex>/<30 hex>`, and only if absent: an existing object with the same md5 is kept, and other content at that path is an error. A DAG run creates no DVC version, so nothing else puts these files there. Nothing in git references them either, so **`dvc gc -c` must never run against this remote.**
   - **`last_final_label_date`** is the earlier of as_of minus 3 days and the newest day in `labels.parquet`. A day's labels are final 48 hours after the day ends; the contract's fixture gives 2026-11-22 for 2026-11-25.
   - **Other fields.** The commit is read from the `.git` files, which are mounted read-only, so no git binary is needed. The catalog hash is taken over the catalog's LF bytes.
   - **Publishing.** `manifests/<as_of>.json` is written first, then the same bytes to `manifests/latest.json`, which never moves back to an older as_of.
   - **A rerun** of an evening replaces its manifest, and so its `data_version`. The scoring task hashes the manifest as it is in the bucket, so the forecast carries the version that is there.

6. **Scoring**, as `docs/contracts/predict.md` defines it:
   - **The service URL.** `predict_url` in `Data-Pipeline/config/evening_run.csv` is `brt-model`'s URL. It is committed, because it is not a secret: the service accepts only `airflow-vm-sa`. It stays blank until #161 deploys, and the task fails naming the setting while it is blank.
   - **Authentication.** The request carries an ID token from the metadata server, with the service URL as its audience.
   - **Timeout and retries.**
     - Each try may take 120 seconds.
     - A 5xx, a 429, a timeout or a lost connection is retried after 30, 60, 120 and 240 seconds: five tries over about 7.5 minutes. A cold start or a model still loading is a 503.
     - A 401 or 403 fails at once and names `roles/run.invoker`. Any other 4xx fails at once with the service's detail.
   - **Checks before sending.**
     - The rows file must still have the md5 the manifest names, so the manifest names exactly what was scored.
     - It must hold the 24 units once each, for one as_of and forecast date.
     - The request is validated against the contract's own schema (`docs/contracts/` is mounted read-only into the Airflow services).
     - **When any model input is null in more than half of tomorrow's rows, scoring stops**, and the alert names each such column with its count. A model scored mostly on missing inputs is not a forecast to send.
   - **Checking the response** against its schema and the contract's rules: one prediction per row in order, the same units and dates, the `data_version` echoed, one model, and `prediction = 1` exactly when `probability >= threshold`.

7. **The forecast file.**
   - **Format:** 24 rows with the contract's Arrow schema and its `contract=forecast/v1` metadata, written with pyarrow, with `issued_at_utc` in whole seconds.
   - **Where it goes:** it is kept beside the rows and written to `forecasts/<forecast_date>.parquet`.
   - **Reruns:** a rerun replaces it (last write wins). The bucket keeps the old generation for 30 days.
   - **No partial forecast:** no forecast file is written without a full, checked response.

8. **The 20:00 check.** A second DAG, `forecast_check`, runs at 20:00 America/New_York.
   - If `forecasts/<the Boston date + 1>.parquet` is missing, its task fails, and `on_task_failure` posts one Slack alert.
   - It skips unless live.
   - A stopped VM runs neither DAG. For that case, the monitoring workflow's freshness alert covers it (#169, from `issued_at_utc`).

9. **The summary.** After scoring, one Slack message per evening run, through the alerts webhook under its own title, "Evening forecast". It gives:
   - the forecast date;
   - how many neighborhoods are flagged, and which;
   - the model version and its MLflow run;
   - the data version;
   - the start time in Boston and the run's duration;
   - the forecast file's URI.

   It is logged at INFO, not as an alert. Without a webhook it stays in the task log.

10. **Code on the VM.** The VM pulls `main` at 17:30 Boston and rebuilds the stack only when the stack's inputs changed: #162 part 1, and ADR-029's note of 2026-11-25.

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Airflow's own timetable at 18:05 Boston, a separate 20:00 DAG, a live switch from the VM's `.env`, in-task retries, validation against the mounted contract schemas (chosen)** | About $1 a day for the VM while live (ADR-029); Cloud Run's free tier covers 24 rows a night | Two DAGs, four small modules, one compose change | A failure at any step fails its task and alerts Slack; a missing forecast alerts at 20:00; a stopped VM is caught by the monitoring workflow |
| A host timer that triggers `daily_pipeline` | $0 | Low | Two schedulers to keep in step; the run's `run_after`, and so its as-of date, then depends on when the trigger ran |
| The 20:00 check as a sensor task inside `daily_pipeline` | $0 | Low | It holds one of four LocalExecutor slots for up to two hours; there is no triggerer for a deferrable sensor (ADR-016); a run that fails early never reaches it, so it cannot report the missing forecast it exists for |
| The 20:00 check in a scheduled GitHub workflow | $0 | Low | GitHub starts scheduled workflows minutes to an hour late. It is the monitoring owner's workflow (#163), and its freshness alert already covers a stopped VM |
| Strictness and the evening tasks keyed on the run type (scheduled or manual) | $0 | None | A manual rerun on the VM after a failure would skip the strict push and the forecast; a laptop run unpaused by mistake would call the cloud |
| The live switch as an Airflow Variable | $0 | Low | Set by hand; lost when the VM or its Postgres volume is replaced (ADR-016) |
| Version the evening files with `dvc add` and `dvc push` inside the DAG | $0 | Medium | It writes `.dvc` and `dvc.lock` changes into the VM's checkout, so the checkout drifts from `main` and the code sync's fast-forward pull fails |
| The contract's null rules copied into the scoring code | $0 | Low | Two copies of the contract drift apart; the read-only mount keeps one |
| Airflow task retries for `/predict` | $0 | Low | It retries the whole task on any error, a 422 included, which cannot succeed; the in-task loop retries only what can |
| `predict_url` in Secret Manager and the rendered `.env` | Under $0.10 a month | Medium | A Terraform slot and a startup rerun for a value that is not secret, and a change of URL would skip review |

## Consequences

- **Cost.** The VM runs 24/7 from the first live evening through Dec 8, at about $1 a day (ADR-029).
- **Bucket growth.** Each evening adds its three files under their md5 (labels and features are about 15 MB together, the rows a few KB, and an unchanged file is not uploaded again), a manifest and a forecast file. Replaced objects are kept as noncurrent versions for 30 days (`Deployment/terraform/storage.tf`).
- **Timing.**
  - Every evening is a full build, 26 to 50 minutes on the VM (runbook, "Run a DAG from the CLI"), so the forecast lands around 18:35 to 19:05 and the 20:00 check leaves about an hour of margin.
  - The first live evening's memory low, recorded against ADR-031's 450 MiB line in #162 part 3, decides whether `validate_processed` joins `heavy_memory` too.
- **Manual triggers and reruns.**
  - A manual trigger before 18:00 Boston is not an evening run: no rows, no manifest, no forecast.
  - A rerun of an evening replaces its manifest, `data_version` and forecast file. Readers key on `issued_at_utc` and `data_version`, as `forecast-file.md` says.
- **Containers.** `docker-compose.yaml` mounts `docs/contracts/` read-only into the Airflow services and passes `BRT_EVENING_RUN`. The code sync (part 1) rebuilds and recreates the stack after that merge.
- **No cloud elsewhere.** No laptop, CI or grader run of `daily_pipeline` needs the cloud: without the switch every evening task skips.
- **`dvc gc -c` must never run against `gs://boston-resilience-twin-data/dvc`.** It would delete every evening file, since no commit references them.

## Revisit when

- The first `/predict` try of an evening often fails on a cold start: set `brt-model`'s minimum instances to 1 and price it (#161).
- The evening run gets long enough to threaten the 20:00 check, or the forecast must be out earlier.
- A second forecast a day is added, or the cutoff moves.
- A consumer needs the evening files as DVC versions, not only as content-addressed objects.
- The evening run leaves airflow-vm, for a managed scheduler.
