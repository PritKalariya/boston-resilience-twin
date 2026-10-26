# Status

Updated: 2026-10-25 by Kirtan. Edit this file in the PR that changes the state.

## Current milestone
Week 2: Processing (due 2026-10-18)

## Open work
| Issue | Owner | Due | State | Note |
|---|---|---|---|---|
| #1 Harden repo and team workflow | Prit | 2026-10-10 | in progress | |
| #2 Local dev environment and Python pin | Punith | 2026-10-13 | done | PR #55 merged. `docker compose up` runs Airflow 3.3.2 (LocalExecutor, Postgres 16); ADR-013 pins Python 3.12 |
| #22 Daily DAG skeleton | Punith | 2026-10-18 | done | PR #72 merged. `daily_pipeline` now: 21 tasks; 8 run scripts (static layers, raw validation, legacy and new 311, combine, spatial join, daily counts, processed validation), plus `check_range`, `merge_legacy_311` (#37) and `push_data` (#19); 10 are placeholders until #6, #7, #8, #16, #35, #32; results below. Runs on the #69 image (ADR-022), no manual installs |
| #36 Logging and error handling across tasks | Punith | 2026-10-22 | done | PR #78 merged. Every task logs run id, rows in and rows out; `ingest_*` tasks retry twice; missing or empty inputs fail before the script runs; a failed task writes the W3-3 alert to its log until #34 delivers it; results below |
| #37 Gantt analysis and optimization | Punith | 2026-10-24 | in review | Legacy years parsed side by side (4 at a time, `LEGACY_PARALLEL` in `.env` to change) and merged: full build 124 s to 73 s, same outputs, peak 3.11 GiB for the whole stack, under the README's 4 GB; Gantt charts in the README; results below |
| #3 Terraform project 1 | Prit | 2026-10-11 | done | PR #30 merged; ADR-015 accepted |
| #4 API access and secrets | Kirtan | 2026-10-11 | in progress | `docs/data-access.md` has one check per API (NOAA CDO, Census, NWS). NOAA and Census keys stored and checked (200, 2026-10-20); `nws-user-agent` slot added in `secrets.tf`, waiting on Prit's review and apply, then its value and NWS check. Keys reach the Airflow containers from `.env` (`NOAA_CDO_TOKEN`, `CENSUS_API_KEY`, `NWS_USER_AGENT`); `credentials.require` fails a task naming a missing key (checked in the scheduler container, 2026-10-23). The pipeline needs no Census key so far (W1-9) |
| #5 Fix legacy 311 parser and re-measure counts | Srikar | 2026-10-11 | done | PR #45 merged; results below |
| #11 Decision: live baselines for the new 311 format | Punith | 2026-10-13 | done | PR #63 merged; ADR-005: baseline = same weeks of the previous 3 years, both systems, by storm group |
| #53 Policy for legacy rows after their category's switch date | Punith | 2026-10-20 | done | PR #71 merged. ADR-021: keep. Setting `after_switch_policy` in `Data-Pipeline/config/processing.csv`, for #15 to apply; results below |
| #13 Format mapping, legacy to new 311 | Srikar | 2026-10-14 | done | PR #52 merged; results below; new-system clock in ADR-020; flooding gap needs a team call |
| #14 Spatial joins | Alekhya (built by Srikar for the Oct 25 deadline) | 2026-10-16 | in review | every request tagged with tract and unit by coordinates; 99.14% placed; results below |
| #15 Daily counts | Srikar | 2026-10-17 | in review | neighborhood x day x storm group, zero-filled; 215,701 requests counted and reconciled; results below |
| #20 Airflow on the VM | Prit | 2026-10-18 | done | PR #59 merged. Airflow stack on airflow-vm (LocalExecutor, Postgres 16; ADR-016); UI by SSH tunnel only; VM stopped. Runbook: "Airflow on the VM". After #58 merges, run the update step in the runbook on the VM |
| #23 Write ADRs 001, 009, 010, 011, 012 | Prit | 2026-10-18 | done | PR #65 merged; ADRs in docs/decisions/ |
| #29 Verify teammate IAM | Prit | 2026-10-15 | done | PR #51 merged. All 6 members granted, plus `roles/iam.serviceAccountUser` on `airflow-vm-sa` (scoped) for OS Login. Teammate start and SSH verified; VM stopped. |
| #49 Decide who gets sudo on the Airflow VM | Prit | 2026-10-17 | in review | Owner call: owner plus Srikar get `roles/compute.osAdminLogin`, bound to the `airflow-vm` instance only, through the new `vm_admins` variable. Plan-only; apply from main after merge. |
| #50 Let Kirtan add secret values | Prit | 2026-10-16 | done | PR #54 merged. secretVersionAdder for `secret_writers` on both secret slots (secret level); ADR-019. |
| #48 Lifecycle rule for noncurrent versions on the data bucket | Prit | 2026-10-18 | done | PR #56 merged; noncurrent versions in the data bucket deleted after 30 days |
| #9 Static layers | Alekhya | 2026-10-13 | done | PR #62 merged; results below |
| #10 Decision: lock the modeling unit | Alekhya | 2026-10-13 | done | PR #66 merged; ADR-004 proposed (team sign-off pending): neighborhood x day, 24 tract-built neighborhoods, surge in any storm group |
| #17 Validation suites (Great Expectations) | Alekhya | 2026-10-18 | in review | `validate_raw` and `validate_processed` run `validate_data.py` (GE 1.24.1); all raw and processed checks pass on the 2026-10-20 data; review fixes from Kirtan in; injected UTC shift and missing column fail by name; results below |
| #31 Repo layout and pipeline pins | Prit | 2026-10-16 | done | PR #58 merged; ADR-017: one shared Airflow stack at the root, each component mounts its `dags/` at `/opt/airflow/dags/<component>` in snake_case; `Data-Pipeline/requirements.txt` pinned, 94 tests pass in a clean Python 3.12 venv |
| #21 CI v1 | Kirtan | 2026-10-16 | in review | `.github/workflows/ci.yml`: Python 3.12 check, `ruff check .` (errors only), tests, on every PR and push to main; no secrets; `conftest.py` blocks network in tests (loopback and Unix sockets allowed, so `socketpair()` and asyncio work on Windows). On main as of 2026-10-25 (992b981, #78 in) plus this change, in plain `python:3.12-slim` as CI runs it: `ruff check .` clean, 234 tests pass and 1 skips (`test_spatial_join.py` needs the static layers on disk), install 24 s. Action pins checked against their tags (checkout v7.0.1, setup-python v7.0.0). Required-check setting needs a repo admin (runbook, "CI") |
| #69 Airflow image with every pipeline dependency | Prit | 2026-10-22 | done | PR #74 merged; ADR-022: every Airflow service runs `brt-airflow:local` from `airflow.Dockerfile`, the ADR-013 base image plus `Data-Pipeline/requirements.txt` under Airflow's constraints. Cold `docker compose up -d --build --wait` 1m37s (image build plus stack start, base image already pulled), cached `docker compose build` 1.0s, image 3.44 GB on disk against the base image's 3.22 GB. Great Expectations 1.24.1, Fairlearn 0.15.0 and dvc[gs] 3.67.1 all dry-run clean against the constraints with pandas 3.0.5. Unblocks #22 and #37 |
| #24 Write ADRs 002, 003 | Srikar | 2026-10-18 | in review | ADR-002 supporting sources, ADR-003 time-based split and feature-history rule; both record scoping decisions (dated 2026-10-04) |
| #19 DVC remote on GCS | Kirtan | 2026-10-15 | done | PR #79 merged. Remote `gs://boston-resilience-twin-data/dvc` set in `.dvc/config` (repo root); dvc 3.67.1 and dvc-gs 3.1.0 pinned. `dvc_push.py` skips with a log line and exits 0 without bucket access (checked with no credentials, 2026-10-23). Round trip with team credentials checked in a scratch repo (push, `dvc pull` in a fresh clone, files match; test objects removed, 2026-10-23). `push_data`, the last task of `daily_pipeline`, runs it; the containers mount `.git` (read-only, since DVC needs a git repo), `.dvc` and `.dvcignore`. `airflow tasks test daily_pipeline push_data` without credentials: skip line, task success; with team credentials: `dvc push to gs://boston-resilience-twin-data/dvc: Everything is up to date.`, task success (2026-10-25). No data tracked yet: stages come with #18 |
| #28 Write ADR-008 | Kirtan | 2026-10-18 | done | PR #86 merged. ADR-008 monitoring tools: Cloud Monitoring for the services (VM, Cloud Run uptime, errors, latency), Evidently for data drift and rolling accuracy and fairness; no Grafana, Prometheus or W&B. Records the scoping decision (dated 2026-10-04); alert thresholds stay open until week 1 of the live run |
| #43 Data card for all sources | Kirtan | 2026-10-23 | in review | `docs/data/data-card.md`, linked from the README: each source's URL, license and attribution, vintage, update frequency, known issues, and coverage of the 2015 to 2023 training years (ADR-003). Facts checked against the sources on 2026-10-25 (Analyze Boston page, NCEI and CO-OPS metadata, the NWS points API) |

## Blocked
None.

## Risks raised
- **DVC stage files in the Airflow containers (#18).** The containers now mount `.git`, `.dvc`, `.dvcignore` and `Data-Pipeline/data`, enough for `push_data`. `Data-Pipeline/dvc.yaml` and its `dvc.lock` are not mounted, so #18 must add them before a task runs `dvc repro` or pushes stage outputs.
- Legacy storm groups cover only 6.9% of legacy requests (210,049 of 3,037,969), against about 15% in the pre-scoping check. The groups now live in `docs/data/category-map.csv` (`storm_group` column) and may be narrower than the first run's. Worth a look before #10 locks the unit.
- **New-system times are Boston local, not UTC.** `open_date` ends in `+00`, but 1,283 cases resubmitted from the legacy system open a median 2.6 minutes before the legacy close time, in both EST and EDT. Real UTC would be 4 to 5 hours later. Converting from UTC would shift every new-system time 4 to 5 hours early and move 1,124 requests (1.6%) to the previous day. The pipeline treats them as local and re-checks this on every run (`map_311.check_clock`). Recorded in ADR-020, accepted by the team on 2026-10-16; CLAUDE.md is updated to match.
- **Flooding has no new-system category.** Legacy flooding requests (mostly Catchbasin) stop after Jul 2026 and nothing in the new system replaces them, so live flooding counts are zero. Affects the target; ADR-005 escalates it, since no baseline can fix a missing source. Needs a team decision (#10, #16) before the target freeze.
- **Snow has not been through the new system yet.** Snow Plowing and Emergency Responder Snow Plowing exist (2 requests), but all of winter 2025 to 2026 went to the legacy system. Unshoveled Sidewalk (48% of last winter's snow requests) has no new category so far. The first snow storm this winter is the real switch for snow_ice; ADR-005 sets a re-check for it (runbook).
- Five new-system categories appeared after the W2-1 run (Illegal Parking 909, Abandoned Vehicles 51, Parking Meter Repairs 13, OPC Public Inquiry 2, Moving Permits 1; export of 2026-10-19). They are `unmapped` until `docs/data/category-map.csv` gets rows for them. None is storm related.
- Neighborhood names differ between systems (23 legacy and 25 new-system names, 16 shared), so #14 places requests by coordinates instead of the name field (ADR-004).
- Animal requests are missing from 2025-06-12 (last legacy) to 2025-08-01 (first day of the new export).
- The local Airflow stack idles at about 0.95 GB of memory (scheduler 397 MiB, API server 294 MiB, DAG processor 193 MiB, Postgres 67 MiB; `docker stats`, 2026-10-16). The e2-medium has 4 GB, so tasks get roughly 2.5 GB once Ubuntu is counted (estimate). #20 should confirm headroom on the VM, and pipeline tasks should avoid loading every legacy year into memory at once.
- On all years (2015 to 2026), `map_311.py` now peaks at about 2.1 GB (was 3.7 GB) and `legacy_311.py` at about 1.8 GB (Python 3.12, pandas 3). Both fit the roughly 2.5 GB left for tasks on the e2-medium, with little headroom, so #22 should run them as separate tasks, not side by side.
- **Four neighborhood lists.** The BPDA boundaries have 26 neighborhoods, the tract-built boundaries and the 2025 population tables have 24, and 311 has 23 legacy and 25 new-system names. ADR-004 (proposed) uses the 24 tract-built neighborhoods and places requests by coordinates, so the 311 field is not used for the unit.
- **Social vulnerability uses 2010 tracts** (180, `GEOID10`), while every other layer uses 2020 tracts (207). Joining it to 2020 tracts needs a crosswalk or an area overlay (#14).
- **Climate Ready Boston flood layers are coastal only.** Most legacy flooding requests are catch basins (stormwater), which these maps do not cover. No stormwater flood layer is on Analyze Boston.
- ~~**The Airflow image lacks the geo packages.**~~ Closed by #69 (ADR-022): the Airflow services now run `brt-airflow:local`, which installs `Data-Pipeline/requirements.txt`, so geopandas, shapely, pyogrio and pyproj are importable in a task. The cost is that a requirements change needs `docker compose up -d --build --wait` before the next run.
- The 2026-10-20 export moves the first new-system date of Broken Park Equipment (not storm related) from 2026-03-10 to 2026-01-09. `docs/data/change-points.csv` is not updated in #10; the next `map_311.py` run in a PR should refresh it.

## W2-5 results (2026-10-25)

Commands: `python Data-Pipeline/scripts/validate_data.py --stage raw` and `--stage processed` (also run through `pipeline_steps.run_step`, the DAG's entry point). Data: Analyze Boston files downloaded 2026-10-20, `311_combined.parquet` and `311_located.parquet` from `map_311.py` and `spatial_join.py`.

- Raw: 144 blocking checks and 142 warning-level checks, all passing, in about 15 s (12 legacy files and the new-system export). Processed: 55 of 55 blocking checks pass in 7 to 10 s. (After review on #83: only the parsers' required columns, coordinates and more than 0.1% missing or repeated case ids block; optional columns and a few bad ids warn.)
- In Airflow (image rebuilt with GE 1.24.1): the `daily_pipeline` slice 2025-01-01 to 2025-12-31 succeeded, all 19 tasks; `validate_raw` 39 of 39 checks and `validate_processed` 55 of 55, results in the slice's `interim/validation/`.
- Worst values against the bounds in `Data-Pipeline/config/validation_bounds.csv`: median open hour 12.4 to 13.5 (bound 10 to 16; a 4 hour shift puts the earliest file at 16.4); coordinates inside Boston 98.0% in 2015 (bound 95%); placed share 97.9% in 2015 (bound 95%); open times parse 100% (bound 99.9%); legacy rows equal the published counts (bound 99%).
- Injected bad files fail by name in the tests (`test_validate_data.py`): a 4 or 5 hour UTC shift (raw legacy, raw new system, combined), a missing column, a `-04` suffix, unparseable dates, a truncated file, swapped coordinates, an unknown unit or storm group, duplicate ids, a future date, lost rows and a low placed share.
- Not yet covered: daily counts (#15, PR #82) and labels (#16) have no suite until they merge; add one in those PRs or a follow-up. #34 can name the failed check from `ValidationFailed` or the JSON; #35 builds statistics on top.

## W1-10 results (2026-10-20)

Commands: `legacy_311.py`, `new_311.py`, `map_311.py` (3,110,129 combined rows), `static_layers.py`, then `compare_units.py`. Data: Analyze Boston files downloaded 2026-10-20, days 2015-01-01 to 2026-10-18. 216,104 of 216,863 storm-related requests (99.7%) are placed by coordinates; 546 have none, 213 fall outside Boston's tracts, 13 are in tract 9815.02, which has no neighborhood.

ADR-005 baseline, analysis grid only (k 2, m 3 shown; #16 freezes the rule):

| unit | columns | empty, train | empty, 2024 to 2026 | surge, train | surge, 2024 to 2026 | columns with no surge |
|---|---|---|---|---|---|---|
| citywide x day x group | 3 | 37.1% | 37.5% | 5.38% | 5.58% | 0 |
| neighborhood x day, pooled | 24 | 59.1% | 55.7% | 3.97% | 5.12% | 1 |
| **neighborhood x day, any group** | 24 | 59.1% | 55.7% | 4.85% | 5.90% | 1 |
| neighborhood x day x group | 72 | 84.7% | 83.4% | 1.65% | 1.99% | 9 |
| tract x day, any group | 207 | 89.6% | 87.6% | 1.32% | 2.27% | 5 |
| tract x day x group | 621 | 96.4% | 95.8% | 0.44% | 0.76% | 197 |

Chosen in ADR-004 (proposed): neighborhood x day, any group. About 22 surges per neighborhood a year; Harbor Islands has none. Flooding alone drives 3.3% of training surges (4.1% in 2024 to 2026). The surge rate is above the spec's 1.3 to 2.9% pre-scoping range at every grid point (3.4% to 6.0% in training), which #16 should weigh when it sets k and m.

## W1-9 results (2026-10-18)

Command: `python Data-Pipeline/scripts/static_layers.py` (add `--download` on a fresh clone). Sources, vintages, licenses and SHA-256 hashes are in `docs/data/static-layers.csv`; the layer list is `Data-Pipeline/config/static_layers.csv`. All layers are PDDL. Vector layers are stored in EPSG:4326 as GeoParquet in `Data-Pipeline/data/interim/static/`.

| layer | rows | source CRS | notes |
|---|---|---|---|
| neighborhoods_bpda | 26 | EPSG:4326 | 1 invalid polygon repaired; not official boundaries |
| neighborhoods_by_tract20 | 24 | EPSG:4326 | whole 2020 tracts |
| tracts_2020 | 207 | EPSG:4326 | |
| crb_social_vulnerability | 180 | EPSG:4326 | 2010 tracts |
| crb_flood_9in_10pct | 1 | EPSG:3857 | 9 inch sea level rise (2030s), 10 percent annual flood; repaired |
| crb_flood_9in_1pct | 1 | EPSG:3857 | 9 inch sea level rise (2030s), 1 percent annual flood; repaired |
| population_2025_neighborhood | 24 | table | 306 columns |
| population_2025_tract | 207 | table | 308 columns; 1 blank export row dropped |
| population_data_dictionary | | document | 286 variables |

ACS demographics come from the city's 2025 population estimates (population on 2025-01-01, characteristics from ACS 2019 to 2023 5-year), so no Census API key is needed. They cover race and ethnicity (B03002), age (B01001), poverty (B17001), household income brackets (B19037), tenure (B25042), vehicles (B08201), language at home (C16001) and disability (S1810). There is no median household income column.

## W2-10 results (2026-10-21)

Commands: `docker compose up -d --wait`, then `airflow dags trigger daily_pipeline` as in the runbook ("Run the data pipeline DAG"). Laptop with Docker Desktop (16 GB); the geo packages were installed by hand in the running scheduler until #69. Raw files: legacy 2015 to 2026 and the new-system export of 2026-10-19.

| run | range | result | wall time | rows written |
|---|---|---|---|---|
| slice-2025b | 2025-01-01 to 2025-12-31 | all 19 tasks succeed | 33 s | combined 280,044 (legacy 275,914, new 4,130), exactly the full build's 2025 rows (pandas `assert_frame_equal`), in `data/slices/` |
| full-build | the defaults (2015-01-01 to latest) | all 19 tasks succeed | 141 s | legacy 3,039,046; new 72,373; combined 3,110,129, the same as the manual run |
| bad-start | start 2014-06-01 | fails at `check_range`, nothing else runs | 6 s | none |
| bad-key | conf key `stat` | fails at `check_range`, nothing else runs | 7 s | none |

A malformed date (2025-13-01) is rejected at trigger time, and the slice left `data/interim/` untouched. Of the 19 tasks, 4 run scripts and 14 are placeholders that log their issue. The slowest task is `parse_legacy_311` (103 s on all years), then `combine_311` (18 s) and `ingest_static_layers` (11 s). In the full build the scheduler container peaked at 3.44 GiB against 0.55 GiB idle (`docker stats` every few seconds), so the tasks added about 2.9 GiB, above the roughly 2.5 GB the VM leaves for tasks; #68 cuts `map_311.py` to about 2.1 GB.

## W3-5 results (2026-10-23)

Runs of `daily_pipeline` on a laptop (the geo packages installed by hand until #69), raw files of 2026-10-19.

| run | checks | result |
|---|---|---|
| slice-2025c | every task logs run id, rows in and rows out | 19 of 19 task logs have the line, for example `run slice-2025c task combine_311: rows in 348,466, rows out 280,044 in 5.0 s` |
| bad-start-36 | a failed task sends the W3-3 alert | `check_range` fails and its log has `ALERT daily_pipeline.check_range failed in run bad-start-36 (try 1): ValueError: start 2014-06-01 is before 2015-01-01, the first training day`; the other 18 tasks do not run |
| missing-new-36 | a missing source fails clearly, with no retry | with `new_system.csv` renamed for the test, `parse_new_311` fails on try 1: `InputError: parse_new_311 cannot start: missing .../raw/311/new_system.csv. Put the new-system export in Data-Pipeline/data/raw/311/ (by hand until #6 lands).`, followed by the alert |

From the DAG in the container: the eight `ingest_*` tasks have 2 retries with a 5-minute base delay, backoff 2.0 and at most 15 minutes, so about 5 to 10 and then 10 to 15 minutes with Airflow's random spread; the other tasks have none, and every task has the failure callback. After Kirtan's review (2026-10-25): a missing or empty input fails at once without retrying (`AirflowFailException`), a step's outputs are deleted before it runs so an old file cannot pass for new output, an empty static download is deleted and fetched again, CSV rows are counted as records the way the parsers report them, and `ingest_static_layers` counts only its configured layers. A corrupt file (a garbage CSV, a non-parquet file) and an empty download (a 0-byte export, a 0-byte static layer) are covered by tests. Checked live on a 2025 slice: every task succeeds, including `spatial_join` (280,044 rows in and out); with `static_layers.csv` hidden for one run, `ingest_static_layers` fails on try 1 with the ALERT line and does not retry.

## W3-6 results (2026-10-24)

Two full builds on a laptop (Docker Desktop with 16 GB, `brt-airflow:local`, #68 in, raw files of 2026-10-19), `docker stats` sampled about every 2.5 s. Gantt charts and the full table are in the README ("Pipeline performance").

| run | whole run | legacy 311 | slowest tasks | peak `docker stats`, scheduler / whole stack |
|---|---|---|---|---|
| full-before | 124 s | 95.1 s in one task | `parse_legacy_311` 95.1 s, `ingest_static_layers` 12.1 s, `combine_311` 9.4 s | 1.93 / 2.52 GiB |
| full-after | 73 s | 45.1 s: 12 year tasks, 4 at a time, in 40.1 s, then the merge 5.0 s | year tasks up to 16.8 s, `ingest_static_layers` 10.4 s, `combine_311` 8.4 s | 2.49 / 3.11 GiB |

The two runs write identical `legacy_311.parquet`, `new_311.parquet` and `311_combined.parquet` (3,110,129 rows). The scheduler cgroup's `memory.peak` was 2.52 GiB after the first run and 2.59 GiB after both, so the stack stayed under 3.3 GiB, below the README's 4 GB. At the peak, 1.95 GiB was the tasks' own memory and 0.1 GiB file cache. This also settles the earlier 3.44 GiB against 958 MiB question: before #68, `map_311.py` itself needed about 3.7 GB.

After Srikar's review (2026-10-25): merged 36-task-logging (Kirtan's fixes, and main with the real `spatial_join`), so the per-year parse and `merge_legacy_311` are blocks in `step_plan` too and their outputs are deleted before they run. `LEGACY_PARALLEL` comes from `.env` through `docker-compose.yaml`: 4 when unset, and anything but a whole number of 1 or more stops the DAG from importing. In the container, `parse_legacy_311` has `max_active_tis_per_dagrun` 4 by default and 2 with `LEGACY_PARALLEL=2`. A 2024 to 2025 slice succeeds with two year tasks and the merge, and its `legacy_311.parquet` (582,849 rows) and `311_combined.parquet` (586,800 rows) equal the full build's rows for those days (`assert_frame_equal`).

## W1-11 results (2026-10-19)

Command: `python Data-Pipeline/scripts/live_baselines.py` (plus `--window-days 84` and `--drop-after-switch`, same verdicts). Data: legacy files 2015 to 2026 and the new-system export downloaded 2026-10-19, combined with `map_311.py`; last full day 2026-10-17; Logan GHCN-D with WSF5 gusts.

| series | switch | step after/before vs normal years | replay, method A (k 1.5) | verdict |
|---|---|---|---|---|
| tree | 2025-10-27 | 0.34 vs 0.37 to 0.70 (lower, -31%); monthly 0.82 of the previous 3 years | quiet days flagged 1.2% (normal 2.5%), weather days 15.2% (normal 16.3%) | pass |
| Street Light Outage | 2025-10-22 | 1.23 vs 0.84 to 1.34 (no shift) | quiet days 5.6% (normal 4.8%) | pass; trailing methods B and C fail |
| snow_ice | 2026-06-23 | not testable: no new-system winter; 50.4% of last winter's requests have a new category | | re-check at first snow |
| flooding | none | no new-system source | | escalated |

Decision in ADR-005: method A (same day plus or minus 14 days in each of the previous 3 years, both systems, broad storm groups).

## W3-14 results (2026-10-21)

Command: `python Data-Pipeline/scripts/after_switch.py`. Data: legacy files 2015 to 2026 and the new-system export of 2026-10-19, combined with `map_311.py`; last full day 2026-10-17; static layers downloaded 2026-10-21.

2,640 legacy rows (0.087%) were opened on or after their category's first new-system day, in 27 categories (20 with more than one row), with windows of up to 111 days. 27 are storm-related (Pruning Request); snow_ice and flooding have none so far. Each cell is the per-neighborhood daily count over the same weeks of the previous 3 years, with the surge rate (k 2, m 3) in brackets.

| category (window) | legacy / new-system rows in window | before | after, keep | after, drop |
|---|---|---|---|---|
| Improper Trash Storage (48 days) | 919 / 1,096 | 1.13 (5.21%) | 0.66 (3.56%) | 0.36 (0.78%) |
| Illegal Graffiti (32 days) | 595 / 7 | 1.24 (4.17%) | 1.68 (4.56%) | 0.02 (0%) |
| Park Groundskeeping (111 days) | 445 / 10 | 0.88 (1.80%) | 0.78 (0.90%) | 0.02 (0%) |
| Pruning Request, tree (109 days) | 27 / 179 | 0.80 (1.57%) | 0.17 (0%) | 0.14 (0%) |

Storm labels (ADR-004 unit, 94,512 labeled cells): drop changes 1, exclude_from_baselines none. Snow Plowing and Emergency Responder Snow Plowing switched on 2026-06-23 with 2 test requests and held 50.5% of last winter's legacy snow requests, so drop could delete half of this winter's snow signal. Decision in ADR-021: keep.

## W2-3 results (2026-10-24)

Command: `python Data-Pipeline/scripts/daily_counts.py` after `spatial_join.py`. `Data-Pipeline/data/processed/daily_counts.parquet` has one row per unit, day and storm group: 24 units x 4,301 days (2015-01-01 to 2026-10-10, the last full day of both systems) x 3 groups = 309,672 rows, 84.5% of them zero. It runs in about 3 seconds and peaks at 1.1 GB.

How the total reconciles (`after_switch_policy` keep, so nothing is removed for after_switch):

| step | requests |
|---|---|
| rows in the combined table (3,106,511 parsed, minus 1,283 resubmitted copies) | 3,105,228 |
| unmapped category | -57 |
| not storm-related | -2,888,393 |
| no coordinates | -546 |
| City Hall placeholder | -291 |
| outside Boston's tracts | -213 |
| tract 9815.02, in no neighborhood | -13 |
| after the last full day | -14 |
| counted | 215,701 |

By storm group: snow_ice 122,585, tree 86,499, flooding 6,617. The total matches #66's comparison once its 291 placeholder rows are taken out (216,019 placed, less 291, 13 and 14).

## W2-2 results (2026-10-24)

Command: `python Data-Pipeline/scripts/spatial_join.py` after `map_311.py` and `static_layers.py`. Every request is placed by its coordinates in a 2020 tract and in the ADR-004 neighborhood built from whole tracts; the 311 neighborhood field is not used. 3,078,578 of 3,105,228 requests (99.14%) are placed in the 24 units. The 253,253 distinct points are placed once each, so the run takes about 4 seconds and peaks at 1.8 GB.

| no unit because | requests |
|---|---|
| no coordinates | 18,594 |
| City Hall placeholder | 4,849 |
| outside Boston's tracts | 2,908 |
| tract 9815.02, in no neighborhood | 299 |

Share with no unit, by year and system (the script also prints the reason columns):

| year | system | requests | no unit | share |
|---|---|---|---|---|
| 2015 | legacy | 202,066 | 4,146 | 2.05% |
| 2016 | legacy | 210,483 | 3,683 | 1.75% |
| 2017 | legacy | 245,475 | 2,505 | 1.02% |
| 2018 | legacy | 257,610 | 2,528 | 0.98% |
| 2019 | legacy | 253,782 | 2,260 | 0.89% |
| 2020 | legacy | 246,165 | 2,169 | 0.88% |
| 2021 | legacy | 269,665 | 1,845 | 0.68% |
| 2022 | legacy | 272,931 | 1,532 | 0.56% |
| 2023 | legacy | 307,791 | 1,736 | 0.56% |
| 2024 | legacy | 306,756 | 1,544 | 0.5% |
| 2025 | legacy | 275,914 | 1,721 | 0.62% |
| 2025 | new | 4,130 | 64 | 1.55% |
| 2026 | legacy | 188,048 | 794 | 0.42% |
| 2026 | new | 64,412 | 123 | 0.19% |

- **City Hall placeholder.** 4,808 legacy requests with the 311 neighborhood "Boston", and 41 new-system requests at "1 City Hall Plz" (mostly animal requests), all sit on one point (42.360295, -71.05827): the default for a request with no address. Placed by coordinates, they would all land in Downtown (291 of them storm-related; on #66 the 290 legacy ones added 4 Downtown training surges). They now count as no location. The point is in `Data-Pipeline/config/placeholder_locations.csv`.
- **Swapped coordinates.** 11 legacy requests (2015 to 2020, none storm-related) have latitude and longitude the wrong way round; all 11 land in a Boston tract once swapped back, so they are swapped and placed.
- **Hand-checked points.** 12 landmarks, one per neighborhood, from Fenway Park to Cleary Square: every tract matches the Census Bureau geocoder (2020 tracts) and every unit matches the landmark's neighborhood. They are a test in `test_spatial_join.py`.

## W2-1 results (2026-10-16)

Commands: `python Data-Pipeline/scripts/legacy_311.py`, `python Data-Pipeline/scripts/new_311.py`, then `python Data-Pipeline/scripts/map_311.py`. Data: legacy files 2015 to 2026 and the new-system export (2025-08-01 to 2026-10-12).

### Category map

`docs/data/category-map.csv` lists all 197 legacy types and 71 new-system categories. Each maps to a shared `category` (the new-system name where one exists) and a `storm_group`. A category not in the file maps to `unmapped` and is counted, so a new city category shows up in the run output instead of disappearing. New tree categories take the group of the legacy types that feed them, so `tree` means the same thing before and after the switch.

| system | rows | same | renamed | merged | split | legacy or new only | unmapped | excluded |
|---|---|---|---|---|---|---|---|---|
| legacy | 3,036,686 | 126,864 | 1,169,732 | 220,374 | 179,661 | 1,340,054 | 0 | 1 |
| new | 68,542 | 2,578 | 32,867 | 12,496 | 12,104 | 8,441 | 56 | 0 |

The 56 unmapped new-system rows have a blank category in the export. The excluded legacy row is "Test Case A". 1,283 legacy rows that were resubmitted to the new system are dropped, so those cases count once. Another 342 legacy rows name a new case that is not in the export (56 predate it); they are kept, since the legacy row is the only copy.

2,519 legacy rows were opened on or after their own category's switch date (27 categories, 20 with more than one row, up to 110 days after). Nothing links them to a new-system case, so they are kept and marked with the `after_switch` column. What to do with them is #53.

| storm group | legacy | new |
|---|---|---|
| snow_ice | 123,167 | 2 |
| tree | 80,227 | 6,727 |
| flooding | 6,655 | 0 |

Tree volumes carry over the Oct 2025 switch at a similar level (Mar 2025: 291 legacy, Mar 2026: 340 new; Feb 137 and 144).

### Time zone

Both systems are stored as naive Boston wall-clock time. Legacy times are already local. New-system times are kept as published (see Risks). Since Aug 2025 the median request hour is 12.7 in legacy and 13.5 in the new system; converting the new times from UTC would put it at 9.7, with 32% of requests before 7am.

### Change points

`docs/data/change-points.csv` has the first new-system date for each shared category and storm group. Departments switched in waves: animals by 2025-08-01, street lights 2025-10-14 to 22, trees 2025-10-27, parks 2025-11-21 and 2026-03-10, public works 2026-06-23, needles 2026-09-08, signs and signals 2026-09-29. Storm groups: tree 2025-10-27, snow_ice 2026-06-23 (no snow since), flooding none.

## W1-5 results (2026-10-13)

Commands: `python Data-Pipeline/scripts/legacy_311.py` then `python Data-Pipeline/scripts/measure_311.py`. Data: legacy yearly files 2015 to 2026 from Analyze Boston, Logan GHCN-D (USW00014739). Last full day 2026-10-10.

### Row counts

Likely cause of the first-run loss: `open_dt` mixes whole seconds with 1 to 3 fractional digits. `pd.to_datetime` without a format infers it from the first row and turns the other variant into NaT. Parsing with `format="ISO8601"` fixes it, and the fixed parser loses no rows.

The first-run code is not in the repo, so the "inferred format" column below re-creates that bug rather than measuring the first run itself. It loses 1,474,743 of 3,037,969 rows (48.5%). That does not fully match the spec: the pre-scoping check counted 833,700 rows for Jan 2024 to early Oct 2026, close to the 840,543 legacy plus new-system rows now, so that run did not lose half its rows in that window. Treat "about a third" as unconfirmed.

| file | published | parsed | inferred format lost | now lost |
|---|---|---|---|---|
| 2015 | 202,066 | 202,066 | 76,212 (37.7%) | 0 |
| 2016 | 210,483 | 210,483 | 87,317 (41.5%) | 0 |
| 2017 | 245,475 | 245,475 | 113,213 (46.1%) | 0 |
| 2018 | 257,610 | 257,610 | 134,306 (52.1%) | 0 |
| 2019 | 253,782 | 253,782 | 132,819 (52.3%) | 0 |
| 2020 | 246,165 | 246,165 | 123,530 (50.2%) | 0 |
| 2021 | 269,665 | 269,665 | 136,362 (50.6%) | 0 |
| 2022 | 272,931 | 272,931 | 146,023 (53.5%) | 0 |
| 2023 | 307,791 | 307,791 | 152,894 (49.7%) | 0 |
| 2024 | 306,756 | 306,756 | 136,898 (44.6%) | 0 |
| 2025 | 276,093 | 276,093 | 90,898 (32.9%) | 0 |
| 2026 | 189,152 | 189,152 | 144,271 (76.3%) | 0 |

The new-system file has 68,542 records, matching the published count. It is not in the numbers below, since its categories need the #13 mapping.

### Sparsity

Storm-related legacy requests over 17 neighborhoods. "South Boston", "Allston", "Brighton", "Chestnut Hill" and "Mattapan" are folded into the city's combined names; "Boston" and blank (6,503 storm rows) count as no neighborhood.

| window | unit | cells | empty |
|---|---|---|---|
| 2015 to 2026 | neighborhood x day | 73,117 | 48.8% |
| 2015 to 2026 | neighborhood x day x group | 219,351 | 80.7% |
| 2024 to 2026 | neighborhood x day | 17,238 | 56.4% |
| 2024 to 2026 | neighborhood x day x group | 51,714 | 83.4% |

The first run's 77% is closest to neighborhood x day x group; with full rows it is 81 to 83%. Dropping the group split halves it.

### Storm ratios

Citywide daily counts on event days over the mean of control days in the same month. Control days within 3 days after an event are skipped, since cleanup requests keep the baseline high. Snow day: SNOW >= 25 mm, control SNOW = 0. Heavy rain: PRCP >= 25.4 mm, control PRCP < 2.5 mm. Heat: TMAX >= 32.2 C, control cooler Jun to Sep days.

Thresholds and controls are this re-measure's own; the first run's definitions are not recorded, so its ratios are shown for reference, not as a like-for-like comparison. New-system rows (Aug 2025 onward) are not included yet.

| check | event days | same day | next day | first run |
|---|---|---|---|---|
| snow_ice, snow days | 106 | 2.11x | 4.42x | 4.9x |
| flooding, heavy rain | 120 | 6.31x | 2.14x | 2.5x |
| tree, heavy rain | 120 | 1.43x | 1.26x | 1.7x |
| all requests, heat | 197 | 1.03x | 1.04x | 1.13x |

2015 to 2026 shown. For 2024 to 2026 (the first-run window): snow 1.45x / 4.36x, flooding 9.38x / 2.30x, tree 1.68x / 1.15x, heat 1.00x / 1.00x. Snow requests peak the day after the storm, flooding on the day. Heat gives almost no signal on total requests.
