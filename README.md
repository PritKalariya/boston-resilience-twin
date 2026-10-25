# Boston Resilience Twin

Forecasting where Boston's 311 service demand will rise next, and checking that the forecast works fairly across neighborhoods.

IE7374 MLOps, Northeastern University, Fall 2026.

## What it does

- Forecasts each evening which Boston neighborhoods will see a surge in storm-related 311 requests the next day.
- Grades every forecast against the next day's 311 reports, tracks fairness across neighborhoods, detects drift, and retrains automatically.
- Core innovation: a fairness-aware retraining controller that decides when and how to retrain, compared against fixed schedules.

## Repository structure

```
boston-resilience-twin/
├── Data-Pipeline/          Airflow DAGs, scripts, tests, DVC
├── Model-Development/      training, tuning, evaluation, bias checks, SHAP
├── Retraining-Controller/  replay environment, policy, baselines
├── Agents/                 response agent, critic, agent evals
├── Deployment/             Dockerfiles, Terraform, Cloud Run configs
├── Monitoring/             drift jobs, dashboards, alert rules
├── Frontend/               digital twin map and dashboards
├── .github/workflows/      CI/CD pipelines
├── docs/                   project status, decision records, runbook
├── docker-compose.yaml     shared Airflow stack for every component (ADR-017)
└── .env.example            local dev overrides only
```

## Setup

```bash
git clone https://github.com/PritKalariya/boston-resilience-twin.git
cd boston-resilience-twin
```

### Local Airflow stack

`docker-compose.yaml` runs Airflow 3.3.2 (LocalExecutor) with a Postgres 16 metadata database. The project uses Python 3.12 everywhere (ADR-013).

**Prerequisites**
- Docker Desktop (Windows uses the WSL 2 backend) or Docker Engine 25+ with the Compose plugin. Give Docker at least 4 GB of memory; 8 GB is better.
- Python 3.12 for running scripts and tests outside Docker (see `.python-version`).

**Start**

On Linux (the VM too), first save your user id in `.env` so the files Airflow writes to `Data-Pipeline/logs/` stay owned by you. macOS and Windows skip this step and need no `.env`:

```bash
echo "AIRFLOW_UID=$(id -u)" > .env
```

Then, on every system:

```bash
docker compose up -d --build --wait
```

The first start downloads the base images, builds the Airflow image and sets up the database. With the base image already pulled that took 1m37s on a laptop; allow a few minutes when Docker has to download it too. Later starts take under a minute. `docker compose ps -a` should show four services as `(healthy)` and `airflow-init` as `Exited (0)`.

**The Airflow image.** Every Airflow service runs `brt-airflow:local`, built from `airflow.Dockerfile`: the `apache/airflow:3.3.2-python3.12` base image (ADR-013) plus `Data-Pipeline/requirements.txt`, installed under Airflow's constraints for that image (ADR-022). The base image has none of the geo packages, so without it a task that imports geopandas, shapely, pyogrio or pyproj fails.

Rebuild it whenever `Data-Pipeline/requirements.txt` changes, including a change you pulled from `main`:

```bash
docker compose up -d --build --wait
```

DAG and script changes never need a rebuild: those files are bind-mounted, not copied into the image. A rebuild with the requirements unchanged is fully cached and takes about a second.

Open http://localhost:8080 and log in as `airflow` / `airflow`. These defaults are for your own machine only.

**Run the hello DAG.** It is a smoke test that also checks the image runs Python 3.12. New DAGs start paused, so unpause it first:

```bash
docker compose exec airflow-scheduler airflow dags unpause hello
docker compose exec airflow-scheduler airflow dags trigger hello
docker compose exec airflow-scheduler airflow dags list-runs hello
```

The run reaches `success` within a minute. You can also unpause and trigger it from the UI.

**Where things live**
- DAG files: `Data-Pipeline/dags/`. New files are picked up within 30 seconds.
- Task logs: `Data-Pipeline/logs/` (gitignored).
- Pipeline data: `Data-Pipeline/data/` (gitignored). The `daily_pipeline` DAG reads `raw/` and writes `interim/`, or `slices/<start>_<end>/` for a shorter date range. How to run it: the runbook, "Run the data pipeline DAG".

**Stop**

```bash
docker compose down      # stop; keeps the database and run history
docker compose down -v   # stop and delete the database (full reset)
```

The containers restart whenever Docker starts (for example after a reboot) until you run `docker compose down`.

**Optional overrides.** `.env` is for local development only. To change a dev default, such as the port, add the line to `.env` using the names in `.env.example`. Never commit `.env`. In Windows PowerShell 5.1, do not create `.env` with `>` or `Out-File`: they write UTF-16 and every compose command then fails.

The VM gets its secrets from Secret Manager (ADR-015), set up in #20. The runbook (`docs/runbook.md`) has the operating steps and fixes for common problems.

**Tests**

```bash
pip install -r Data-Pipeline/requirements.txt
python -m pytest Data-Pipeline/tests
```

Use Python 3.12 (ADR-013): the pins come from the Airflow constraints for 3.12, and on other versions the install can fail or build packages from source. Without a system 3.12, `uv venv --python 3.12 --seed .venv && source .venv/bin/activate` creates one (`.venv/` is gitignored). A test command pinned to Python 3.12 comes with W3-7 (#38).

## Pipeline performance (#37)

Full builds of `daily_pipeline` (2015 to 2026) on a laptop, 2026-10-24: Docker Desktop with 16 GB, the `brt-airflow:local` image, `map_311.py` after #68, and the raw 311 files of 2026-10-19.

**Before.** One `parse_legacy_311` task parsed the 12 yearly legacy files one after another, 95 of the run's 124 seconds:

![Gantt chart before: one long parse_legacy_311 bar](docs/images/gantt-before.jpeg)

**After.** `parse_legacy_311` runs once per year, at most 4 at a time (`LEGACY_PARALLEL`, 4 unless `.env` sets it, the same as Airflow's parallelism; see the runbook), and a new `merge_legacy_311` task joins the years with the rule `legacy_311.read_years` uses for case ids repeated across years. `legacy_311.py` itself is unchanged.

![Gantt chart after: the years side by side, then merge_legacy_311](docs/images/gantt-after.jpeg)

| | before | after |
|---|---|---|
| whole run | 124 s | 73 s |
| legacy 311 | 95.1 s (`parse_legacy_311`) | 45.1 s: 12 years in 40.1 s (8.1 to 16.8 s each), then `merge_legacy_311` 5.0 s |
| `ingest_static_layers` | 12.1 s | 10.4 s |
| `combine_311` | 9.4 s | 8.4 s |
| `parse_new_311` | 2.4 s | 2.8 s, after waiting about 10 s for a free slot while 4 years ran |
| peak memory, `docker stats`, scheduler | 1.93 GiB | 2.49 GiB |
| peak memory, `docker stats`, whole stack | 2.52 GiB | 3.11 GiB |

The slowest tasks were `parse_legacy_311` (95.1 s), `ingest_static_layers` (12.1 s) and `combine_311` (9.4 s) before, and the year tasks (up to 16.8 s each), `ingest_static_layers` (10.4 s) and `combine_311` (8.4 s) after.

The outputs did not change: `legacy_311.parquet` (3,039,046 rows), `new_311.parquet` (72,373) and `311_combined.parquet` (3,110,129) are identical in the two runs (pandas `assert_frame_equal`).

**Memory.** Both runs stay under the 4 GB this README asks Docker to have (Setup). `docker stats` was sampled about every 2.5 s. The scheduler cgroup's `memory.peak`, which records the true peak, was 2.52 GiB after the first run and 2.59 GiB after both, so the whole stack stayed under 3.3 GiB. At the peak, 1.95 GiB of the scheduler's memory was the tasks' own and 0.1 GiB was file cache. More years at once would raise the peak, which is why `LEGACY_PARALLEL` caps it. On the 2-vCPU VM the gain will be smaller; #61 measures memory there.

## Data and licenses

Boston 311 data is published on Analyze Boston under the PDDL. NWS, NOAA, and Census data are U.S. government works. Weather data by [Open-Meteo.com](https://open-meteo.com) (CC BY 4.0).
