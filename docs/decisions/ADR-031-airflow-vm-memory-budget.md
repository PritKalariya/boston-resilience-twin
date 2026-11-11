# ADR-031: Memory budget on airflow-vm: a one-slot pool for heavy tasks, per-service memory limits, parallelism 4

- Status: accepted
- Date: 2026-11-10
- Owner: PritKalariya
- Phase: deploy
- Amends ADR-016 by reference: ADR-016's "Revisit when" asked #61 to measure a real run and confirm or lower `core.parallelism`; this ADR records that measurement and what it changed. ADR-016 is not edited.

## Context

Issue #61 (D3) asks for the Airflow memory budget on airflow-vm (e2-medium, 4 GB, no swap; ADR-010, ADR-016). The first full build (#39) hit 42 MB available while `schema_and_statistics` and `bias_slicing` ran together, with no task killed. The acceptance bar is a low of at least 500 MB available during a full build.

Measured in run `mem-baseline-20261110` on airflow-vm, e2-medium, us-east1-b, at main e200b8c: a full build from 2026-11-10 01:59:37 to 02:25:16 UTC (25 min 39 s). A sampler read `MemAvailable` every 1 s and `docker stats` about every 5 s; each container's cgroup `memory.peak` was read afterwards.

- **Host:** 3908 MiB total, no swap, 2491 MiB available at idle. The containers used 968 MiB at idle (scheduler 425, apiserver 269, dag-processor 202, postgres 72), which leaves about 458 MiB used outside containers.
- **Peaks since boot** (`memory.peak`, which includes page cache): scheduler 2805 MiB, dag-processor 446, apiserver 380, postgres 89.
- **Lows by stage** (MiB available):

| Stage | Low |
|---|---|
| ingest, 4 downloads in parallel | 509 at 02:00:05 |
| validate_raw | 1807 |
| legacy parse, 4-wide | 1074 |
| merge, combine, join, counts | 623 (combine_311) |
| labels and features | 1726 |
| validate_processed | 723 |
| schema_and_statistics and bias_slicing overlapping | 81 at 02:20:57 |
| bias_slicing alone | 1514 |
| push_data | 2211 |

- **Ingest attribution:** `ingest_alert_history` adds about 1.1 GiB while it runs. `ingest_static_layers` uses about 1.0 GiB alone.
- **Durations:** `schema_and_statistics` 2 min 53 s, `bias_slicing` 6 min 47 s, `ingest_alert_history` 20 s, `ingest_static_layers` 62 s.
- **Open item:** after the run, available memory dipped to 1820 MiB at 02:37 and to 786 MiB at 02:47:22, with nothing scheduled. No host service peaked above 144 MiB and the journal was empty. The most likely cause is a short spike in the scheduler cgroup between two `docker stats` samples. The re-measure sampler logs the top processes whenever available memory is low, so it can identify this if it recurs.

Measured again in run `mem-20261110` (run 2) on airflow-vm, at `ba08086`, from freshly created containers, following the runbook's re-measure procedure: 2026-11-10 06:03:51 to 06:28:51 UTC (25 min 00 s), against 25 min 39 s for the baseline.

- **Host low:** 463 MiB (485 MB) at 06:14:02. Available memory was under 500 MB for about 3 s (06:14:00 to 06:14:03), while `combine_311` ran alone; its task process was at 1909 MiB RSS. The same window bottomed at 623 MiB in the baseline, so it varies by about 160 MiB between runs.
- **Scheduler:** anonymous peak 2219 MiB, 341 below its 2560m limit. `memory.peak` was 2486 MiB; the limit was never hit (`memory.events` max 0). It stalled for memory 1.58 s (`memory.pressure` full).
- **Other containers:** `oom_kill` 0 in every container. Peaks: dag-processor 414 MiB, apiserver 346, postgres 69.
- **Pool:** the heavy tasks ran one at a time. `schema_and_statistics` took 38 s alone, against 2 min 53 s alongside `bias_slicing` in the baseline.

The budget covers today's stack only: Postgres plus the four Airflow services. mlflow will run under a compose profile that is off by default (#135), so airflow-vm reserves nothing for it.

## Decision

1. **A pool.** `airflow-init` creates the pool `heavy_memory` with 1 slot (`airflow pools set`, which updates an existing pool, so a rerun of init exits 0). Four tasks use it: `ingest_alert_history`, `ingest_static_layers`, `schema_and_statistics` and `bias_slicing`. Their names are one constant in `daily_pipeline.py`. Every other task stays in the default pool. The step table in `pipeline_steps.py` carries no pool data, since DVC has no pools.
2. **Limits**, set per service in `docker-compose.yaml` (not in the shared anchor):

| Service | Setting |
|---|---|
| airflow-scheduler | `mem_limit: 2560m` |
| airflow-dag-processor | `mem_limit: 640m` |
| airflow-apiserver | `mem_limit: 512m` |
| airflow-init | `mem_limit: 768m` |
| postgres | no `mem_limit`; `oom_score_adj: -500` |
| test | unchanged, no limit (it runs on the CI runner) |

3. **Parallelism stays 4** (`AIRFLOW__CORE__PARALLELISM`). The 4-wide legacy parse measured 1074 MiB available at its low, against the 500 MB floor, so the cap needs no change. The heavy tasks are serialized by the pool instead.
4. **Postgres gets `oom_score_adj: -500`** and no limit, so if the host runs out of memory the kernel kills a task process before the metadata database.

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Pool of 1 for four heavy tasks, limits, parallelism 4 (chosen)** | None: run 2 was 39 s faster than the baseline, since the heavy tasks no longer contend; $0 | Small: one constant, one init line, limits in compose | A task over its container's limit is killed alone (`oom_kill` in `memory.events`) and the DAG's alert fires; a task that needs more than the scheduler's limit fails every run until the limit or the task changes |
| Chain `schema_and_statistics` before `bias_slicing` | Same effect as the pool for those two; does not cover the ingest overlap (low 509 MiB at 02:00:05, only 9 MiB over the floor) | Small | Fixes the 81 MiB low but leaves the ingest low one new download away from the floor, and a new heavy task needs its own edge |
| Parallelism 1 or 3 | Parallelism 1 runs every task, the 4-wide legacy parse included, one at a time; the added time was not measured | One setting | Slower for tasks that were never a problem (the parse low was 1074 MiB), while the overlap that caused the 81 MiB low can still happen at 3 |
| e2-standard-2 (8 GB) | About $56 a month with disk and IP (machine about $49), against about $31 for e2-medium (ADR-016): nearly double, against a $50 per-member budget | Resize in Terraform | No change in behavior except more room; the budget is the failure |
| Swap | $0 beyond disk | Small | Slow paging on a 2-shared-vCPU VM instead of a clean kill; every task and service slows down while memory is short |
| Trim `combine_311` and `validate_processed` | $0 | Medium: code changes in another owner's area | Their lows (623 and 723 MiB) are already above the floor and each runs alone, so there is nothing to fix |

## Consequences

- Run 2 took 25 min 00 s, 39 s faster than the baseline: the four heavy tasks no longer overlap, so each runs faster (`schema_and_statistics` 38 s alone against 2 min 53 s).
- The measured build low is 463 MiB (485 MB), for about 3 s during `combine_311`, which runs alone: 15 MB short of the 500 MB target, and neither the pool nor the limits change a single task's peak. It is accepted because the scheduler's 2560m limit kills a runaway task inside its cgroup before Postgres is affected. The window varies by about 160 MiB between runs (623 MiB in the baseline). Trimming `combine_311`'s peak is on the Later list for Deployment planning.
- If every service peaked at once (the scheduler at its 2560 MiB limit, the others at their measured peaks, and the 458 MiB outside containers), the total would be about 25 MiB over the host. That is why Postgres gets `oom_score_adj`: the kernel then picks a task, not the database.
- Laptops get the same limits through the shared compose file. The PR's laptop full build passed under them (34 of 34 tasks, oom_kill 0).
- The scheduler's `memory.peak` reads at or near its limit once page cache fills, so it is not the measure.
- The limit sits above the build's anonymous-memory peak, and page cache is reclaimed at the limit.
- On the laptop build in this PR, the scheduler hit its limit 1650 times (`memory.events` max), every time reclaimed (oom 0, oom_kill 0), with 0.97 s stalled for memory in total.
- Run 2 recorded the scheduler's anonymous peak at 2219 MiB, 341 below the limit.
- The baseline's 2805 MiB was measured before the limits and includes page cache.
- The open 02:47 dip (786 MiB available with nothing scheduled) is unexplained. The re-measure sampler logs the top processes when available memory is low and may identify it.
- If `airflow-init` has not run, the pool does not exist. What the scheduler does with a task whose pool is missing is recorded in the runbook ("Memory budget on airflow-vm").
- Cost: none. The re-measure needs the VM running for about 30 minutes, about $0.04 an hour. Stop it afterwards: `gcloud compute instances stop airflow-vm --zone=us-east1-b`.

## Revisit when

- A re-measure shows a low below 450 MiB available, confirmed by one re-run (run 2 measured 463 MiB; the 500 MB target was accepted short at that value).
- Any container's `memory.events` shows `oom_kill` above 0.
- A single task needs more than about 1.9 GiB.
- The scheduler's `memory.pressure` full total passes 10 s in one build.
- A service or DAG joins the VM stack. It gets its own `mem_limit` and a re-measure.
