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
- Python, Docker Compose for local development

## Open: do not choose, ask the user
Python version, Airflow executor and metadata DB, API framework (FastAPI is a candidate only), dashboard and map libraries, Gemini tier and per-event cap, alert thresholds.

## Workflow rules
- One issue per task, one assignee, due date, phase label, acceptance criteria.
- The user creates the branch `<issue-number>-<slug>` and does every git write. Never run `git add`, `commit`, `push`, `merge`, `rebase`, `reset`, `stash`, or `tag`. Read-only git (`status`, `diff`, `log`) is fine. Leave all changes uncommitted.
- Finish with a report: files changed, each acceptance-criteria command with its actual output, and anything skipped or uncertain. The user takes it to review before committing.
- Finish every task by updating `docs/status.md`. Update the runbook (`docs/runbook.md`, create if absent) when an operational step changes.
- Do not write ADRs unless the task says so. Owners author them in `docs/decisions/` from the template.
- `docs/` holds project docs only. No scoping PDF, no personal notes.
- No em dashes in docs, comments, or commit messages.

## Data and model rules
- Splits are time-based, never shuffled: train 2015 to 2023, validate 2024, test 2025 to mid-2026.
- A feature is used only if its history covers the training years.
- Observed weather in training makes backtests upper bounds. Validation and test use Open-Meteo archived forecasts (CC BY 4.0, keep attribution).
- The 311 migration is a change point: new-system times are UTC, categories are not one-to-one. The old-to-new category mapping is a versioned file changed only by reviewed PR.
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

- `pip install -r Data-Pipeline/requirements.txt`
- `python Data-Pipeline/scripts/legacy_311.py`: parse legacy 311 files in `Data-Pipeline/data/raw/311/` to `Data-Pipeline/data/interim/legacy_311.parquet`, with per-file drop counts
- `python Data-Pipeline/scripts/measure_311.py`: reconciliation, sparsity, and storm ratio tables (markdown)
- `python -m pytest Data-Pipeline/tests`
- `cd Deployment/terraform && terraform init`: configure the GCS state backend and providers (needs `gcloud auth application-default login`)
- `terraform fmt -check && terraform validate`: format and syntax check, no cloud calls
- `terraform plan -var-file=terraform.tfvars`: read-only dry run against project 1 (`terraform.tfvars` is gitignored; copy from `terraform.tfvars.example`)
- `terraform apply -var-file=terraform.tfvars`: human only, never run by Claude Code
- `gcloud compute instances start|stop airflow-vm --zone=us-east1-b`: VM power; Terraform does not manage run state
