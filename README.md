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
pip install ruff==0.17.2 && ruff check .   # lint, as CI runs it
```

CI (`.github/workflows/ci.yml`) runs the same lint and tests on Python 3.12 for every pull request and every push to main. Tests may not reach the network: `Data-Pipeline/tests/conftest.py` fails any test that tries, so use fixture files or fakes.

Use Python 3.12 (ADR-013): the pins come from the Airflow constraints for 3.12, and on other versions the install can fail or build packages from source. Without a system 3.12, `uv venv --python 3.12 --seed .venv && source .venv/bin/activate` creates one (`.venv/` is gitignored). A test command pinned to Python 3.12 comes with W3-7 (#38).

### Versioned data (DVC)

DVC is installed by `Data-Pipeline/requirements.txt`. The remote is **`gs://boston-resilience-twin-data/dvc`**, a private bucket in project 1 (`.dvc/config`).

**Team members** have read and write access. Log in once per machine, then pull from the repo root:

```bash
gcloud auth application-default login
dvc pull
```

**Without access**, for example a grader, skip `dvc pull`. Rebuilding the data from the public sources with `dvc repro` arrives with #18; until then `Data-Pipeline/dvc.yaml` has no stages and `dvc repro` does nothing. Pushing is skipped too: `python Data-Pipeline/scripts/dvc_push.py` logs `dvc push skipped: ...` and exits 0, and the data stays in the local DVC cache.

The runbook ("Versioned data (DVC)") has the checks and fixes.

## Data and licenses

Boston 311 data is published on Analyze Boston under the PDDL. NWS, NOAA, and Census data are U.S. government works. Weather data by [Open-Meteo.com](https://open-meteo.com) (CC BY 4.0).

The [data card](docs/data/data-card.md) lists every source with its URL, license and attribution, vintage, update frequency, coverage of the training years and known issues.
