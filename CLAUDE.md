# CLAUDE.md: Boston Resilience Twin

IE7374 MLOps team project (Northeastern, Fall 2026). Each evening, forecast per Boston neighborhood whether the next day brings a surge in storm-related 311 requests. Grade every forecast against actual reports, track accuracy and fairness, detect drift, retrain automatically, promote a new model only if it beats the current one. Advisory only: a person reviews every recommendation.

## Read first, every task
1. The issue you were given (its acceptance criteria are your definition of done)
2. `docs/project-spec.md`
3. `docs/status.md`
4. `docs/decisions/`: accepted ADRs bind you. If a task conflicts with one, stop and say so.

`docs/project-spec.md` is read-only context mirrored from project knowledge. Do not edit it; flag a needed change instead.

## Owners
Data pipeline: Srikar. Fairness and data quality: Alekhya. Forecast model, response agent, critic: Piyush. Retraining controller: Punith. Deployment: Prit. Monitoring, map, dashboards: Kirtan. Change another owner's area only in a PR they review.

## Decided stack
- Airflow in Docker on an e2-medium VM (daily DAG, event DAG)
- DVC with a Cloud Storage remote, Great Expectations, Fairlearn
- Seasonal baseline, logistic regression, LightGBM, XGBoost, Optuna, SHAP
- MLflow for tracking only. Vertex AI Model Registry is the only registry.
- Cloud Run: forecast API, agent service, dashboard. Gemini on Vertex AI for the agent and critic.
- GitHub Actions for CI/CD, Terraform for infrastructure
- Cloud Monitoring and Evidently. Slack and email alerts. No Grafana, Prometheus, or W&B.
- Python 3.12 (ADR-013), Docker Compose for local development (Airflow 3.3.2, Postgres 16)
- Layout (ADR-017): one top-level folder per component; the root `docker-compose.yaml` is the shared Airflow stack and mounts each component's `dags/` at `/opt/airflow/dags/<component>`, named in snake_case (`data_pipeline`). A DAG imports helpers in its own folder as `data_pipeline.<module>`, never by bare module name. That import resolves only inside the Airflow containers, so host `python -m pytest Data-Pipeline/tests` cannot import DAG helpers yet.

## Open: do not choose, ask the user
Airflow executor and metadata DB (the local stack in `docker-compose.yaml` uses LocalExecutor and Postgres 16; the ADR comes with #20), API framework (FastAPI is a candidate only), dashboard and map libraries, Gemini tier and per-event cap, alert thresholds.

## Workflow rules
- One issue per task, one assignee, due date, phase label, acceptance criteria.
- The user creates the branch `<issue-number>-<slug>` and does every git write. Never run `git add`, `commit`, `push`, `merge`, `rebase`, `reset`, `stash`, or `tag`. Read-only git (`status`, `diff`, `log`) is fine. Leave all changes uncommitted.
- Finish with a report: files changed, each acceptance-criteria command with its actual output, and anything skipped or uncertain. The user takes it to review before committing.
- Out-of-scope findings: list them in your final report as “Maps to #n” or “Later”. Never propose or draft new issues.
- Finish every task by updating `docs/status.md`. Update the runbook (`docs/runbook.md`, create if absent) when an operational step changes.
- Do not write ADRs unless the task says so. Owners author them in `docs/decisions/` from the template.
- `docs/` holds project docs only. No scoping PDF, no personal notes.
- No em dashes in docs, comments, or commit messages.

## Data and model rules
- Splits are time-based, never shuffled: train 2015 to 2023, validate 2024, test 2025 to mid-2026.
- A feature is used only if its history covers the training years.
- Observed weather in training makes backtests upper bounds. Validation and test use Open-Meteo archived forecasts (CC BY 4.0, keep attribution).
- The 311 migration is a change point: new-system times carry a `+00` suffix but are Boston local (ADR-020), categories are not one-to-one. The old-to-new category mapping is a versioned file changed only by reviewed PR.
- The target unit and surge rule are frozen by ADR before Model Development. Do not redefine them in code.

## Security
The repo is public. Never commit keys, tokens, `.env`, Terraform state, raw or versioned data (DVC holds it), or the scoping PDF. Secrets live in Secret Manager. Keep `.gitignore` current.

## Cloud and cost
- GCP resources come from Terraform only. No console click-ops.
- Two projects (ADR-009). Project 1: VM, Cloud Storage, monitoring, Secret Manager. Project 2: Cloud Run, Artifact Registry, Vertex registry, Gemini. Never put a resource in the wrong project.
- Budget is $50 credits per member, not pooled. The VM (about $31 a month running, about $3 stopped) stays stopped when idle in October and runs 24/7 from Nov 1.
- Never run `terraform apply` or `terraform destroy`. Before any change that creates a resource, print its running cost and the command that tears it down.

## Deadlines that drive priority
Data Pipeline Oct 25. Model Development Nov 15. Deployment Dec 6. Expo requirements Dec 7, 11:59am.

## Commands
Add each command here, in the PR that introduces it. Do not guess commands that are not listed.

- `uv venv --python 3.12 --seed .venv && source .venv/bin/activate`: create and enter a Python 3.12 venv with pip in it (ADR-013); `.venv/` is gitignored
- `pip install -r Data-Pipeline/requirements.txt`: pinned from the Airflow 3.3.2 constraints (the `-c` line). A new package takes the constraints file's version; if the file has none, pin the version the tests passed on.
- `python Data-Pipeline/scripts/legacy_311.py`: parse legacy 311 files in `Data-Pipeline/data/raw/311/` to `Data-Pipeline/data/interim/legacy_311.parquet`, with per-file drop counts
- `python Data-Pipeline/scripts/measure_311.py`: reconciliation, sparsity, and storm ratio tables (markdown)
- `python Data-Pipeline/scripts/new_311.py`: parse the new-system export to `Data-Pipeline/data/interim/new_311.parquet`, with drop counts
- `python Data-Pipeline/scripts/map_311.py`: apply `docs/data/category-map.csv`, combine both systems into `Data-Pipeline/data/interim/311_combined.parquet`, write `docs/data/change-points.csv`
- `python Data-Pipeline/scripts/live_baselines.py`: test the 311 switch dates as change points and replay surge baseline methods across them (markdown for ADR-005); `--window-days 84` and `--drop-after-switch` for sensitivity
- `python Data-Pipeline/scripts/static_layers.py [--download|--refresh]`: load the layers in `Data-Pipeline/config/static_layers.csv` to `Data-Pipeline/data/interim/static/` (EPSG:4326) and write `docs/data/static-layers.csv`; `--download` fetches missing files, `--refresh` all of them
- `python Data-Pipeline/scripts/compare_units.py`: compare candidate modeling units (empty-cell share, surge rate with the ADR-005 baseline) from `311_combined.parquet` and the static tracts and neighborhoods (markdown; about 4 minutes)
- `python Data-Pipeline/scripts/after_switch.py`: legacy rows opened after their category's switch date (`after_switch`) by category, the worst overlap windows and the storm surge labels under each policy (markdown for ADR-021); reads `after_switch_policy` from `Data-Pipeline/config/processing.csv`, needs the static layers (about 1.5 minutes)
- `python -m pytest Data-Pipeline/tests`
- `cd Deployment/terraform && terraform init`: configure the GCS state backend and providers (needs `gcloud auth application-default login`)
- `terraform fmt -check && terraform validate`: format and syntax check, no cloud calls
- `terraform plan -var-file=terraform.tfvars`: read-only dry run against project 1 (`terraform.tfvars` is gitignored; copy from `terraform.tfvars.example`)
- `terraform apply -var-file=terraform.tfvars`: human only, never run by Claude Code
- `gcloud compute instances start|stop airflow-vm --zone=us-east1-b`: VM power; Terraform does not manage run state
- `read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets versions add <secret> --data-file=-; unset VALUE`: add a secret value with no trailing newline and nothing in shell history (see runbook). Human only, never run by Claude Code
- `docker compose config --quiet`: validate `docker-compose.yaml`; no output means valid
- `docker compose up -d --wait`: start Airflow 3.3.2 (LocalExecutor) and Postgres 16 and wait until healthy; UI at http://localhost:8080, login airflow / airflow (local defaults)
- `docker compose ps -a`: service state and health; `airflow-init` shows `Exited (0)` once the database is migrated
- `docker compose logs <service>`: logs of one service, for example `airflow-scheduler`
- `docker compose exec airflow-scheduler airflow dags list-import-errors`: DAG import errors; `No data found` when every DAG imports
- `docker compose exec airflow-scheduler airflow dags unpause|trigger|list-runs hello`: run the hello smoke test (new DAGs start paused)
- `docker compose exec airflow-scheduler python --version`: Python in the Airflow image (ADR-013 pins 3.12)
- `docker compose down`: stop and remove the containers, keep the metadata DB; add `-v` to delete it too (full reset)
