# CLAUDE.md: Boston Resilience Twin

IE7374 MLOps team project (Northeastern, Fall 2026). Each evening, forecast per Boston neighborhood whether the next day brings a surge in storm-related 311 requests. Grade every forecast against actual reports, track accuracy and fairness, detect drift, retrain automatically, promote a new model only if it beats the current one. Advisory only: a person reviews every recommendation.

Current phase: Deployment (Nov 16 to Dec 6): serving, monitoring, drift-triggered retraining, response layer, map and dashboards, live run. Skeleton first: one evening forecast served end to end by Nov 25, then hardening.

## Read first, every task
1. The issue you were given (its acceptance criteria are your definition of done)
2. `docs/project-spec.md`
3. `docs/decisions/`: accepted ADRs bind you. If a task conflicts with one, stop and say so.
4. `docs/CONTRIBUTING.md` and `.github/pull_request_template.md`: how a PR is written, reviewed and merged.

`docs/project-spec.md` is read-only context mirrored from project knowledge. Do not edit it; flag a needed change instead.

## Owners
Contracts, serving and rollouts: Prit. Evening inputs, live data versions, retraining trigger and replay: Srikar. Monitoring, dashboards and alerts: Alekhya. Evening run, rolling split, response agent: Punith. CI and platform hygiene: Kirtan. Forecast map and brief critic: Piyush. Change another owner's area only in a PR they review.

## Decided stack
- Airflow in Docker on an e2-medium VM: `daily_pipeline`, run each evening. No event DAG.
- DVC with a Cloud Storage remote, Great Expectations, Fairlearn
- Seasonal baseline, logistic regression, LightGBM, XGBoost, Optuna, SHAP
- MLflow for tracking only, never started on the VM (#61). Vertex AI Model Registry is the only registry.
- Retraining: the drift-threshold rule (ADR-007) triggers it. The learned controller is cut by the Deployment architecture ADR (#159), which amends ADR-007. `Retraining-Controller/` holds the threshold rule and the replay.
- Cloud Run in project 2: the model service (`Serving/`, FastAPI: `/predict`, `/forecasts/{date}`, and the `Frontend/` map page) and the agent service (`Agents/`). Gemini on Vertex AI for the agent; the critic is a deterministic check, no model.
- Shared formats (the `/predict` request and response, forecast file, data manifest, monitoring output, retrain decision, brief) are defined in `docs/contracts/` (#159). Until it merges, take them from #159's draft PR. Never invent a format another issue reads; change one only in its own PR.
- GitHub Actions for CI/CD, Terraform for infrastructure
- Cloud Monitoring (dashboards and alert policies, project 1) and Evidently (in `Monitoring/`'s own image, never the Airflow image). Slack and email alerts. No Grafana, Prometheus, or W&B.
- Python 3.12 (ADR-013), Docker Compose for local development (Airflow 3.3.2, Postgres 16)
- Layout (ADR-017): one top-level folder per component; the root `docker-compose.yaml` is the shared Airflow stack and mounts each component's `dags/` at `/opt/airflow/dags/<component>`, named in snake_case (`data_pipeline`). A DAG imports helpers in its own folder as `data_pipeline.<module>`, never by bare module name. That import resolves only inside the Airflow containers, so the DAG tests run in the compose `test` service and skip on a host `python -m pytest`.

## Open: do not choose, ask the user
The map library (#172), the Gemini tier and daily call cap (#173), and the alert and retraining thresholds (#170 calibrates them on the replay; never guess them).

## Workflow rules
- One issue per task, one assignee, due date, phase label, acceptance criteria.
- The user creates the branch `<issue-number>-<slug>` and does every git write. Never run `git add`, `commit`, `push`, `merge`, `rebase`, `reset`, `stash`, or `tag`. Read-only git (`status`, `diff`, `log`) is fine. Leave all changes uncommitted.
- Finish with a report: files changed, each acceptance-criteria command with its actual output, and anything skipped or uncertain. The user takes it to review before committing.
- Out-of-scope findings: list them in your final report as “Maps to #n” or “Later”. Never propose or draft new issues.
- docs/status.md is retired. Never recreate or edit it. Status lives in GitHub issues and PRs.
- Update the runbook when an operational step changes: the section the table under Commands names (after #164 splits it, the matching file in `docs/runbook/`). Never create a new runbook file of your own.
- If your change makes a line of this file wrong (a moved doc, a renamed heading, a changed rule), fix that line in the same PR and say so in the PR body.
- Docs cite run ids or dates, never counts that change with every run.
- Do not write ADRs unless the task says so. Owners author them in `docs/decisions/` from the template.
- ADR numbers: the next free number in docs/decisions/ at merge; rename the file if it was taken.
- After pushing fixes to a PR with requested changes: reply on each thread with the commit, then re-request review with `gh api -X POST repos/PritKalariya/boston-resilience-twin/pulls/<number>/requested_reviewers -f "reviewers[]=<login>"`.
- `docs/` holds project docs only. No scoping PDF, no personal notes.
- No em dashes in docs, comments, or commit messages.

## Data and model rules
- Splits are time-based, never shuffled: train 2015 to 2023, validate 2024, test 2025 to mid-2026.
- A feature is used only if its history covers the training years.
- Observed weather in training makes backtests upper bounds. Validation and test use Open-Meteo archived forecasts (CC BY 4.0, keep attribution).
- The 311 migration is a change point: new-system times carry a `+00` suffix but are Boston local (ADR-020), categories are not one-to-one. The old-to-new category mapping is a versioned file changed only by reviewed PR.
- The target unit and surge rule are frozen by ADR before Model Development. Do not redefine them in code.

## Security
The repo is public. Never commit keys, tokens, `.env`, Terraform state, raw or versioned data (DVC holds it), or the scoping PDF. Secrets live in Secret Manager. Keep `.gitignore` current. Never print a secret value; check one is set without echoing it.

In the training image only the three main-only registry steps get Google credentials (`MOUNT_GCP_CREDENTIALS=1`, ADR-035). Never set it for another step, and never mount the workspace into the image.

## Cloud and cost
- GCP resources come from Terraform only. No console click-ops.
- Two projects (ADR-009). Project 1: VM, Cloud Storage (the data bucket: data, the DVC remote, forecasts, manifests and monitoring outputs), monitoring, Secret Manager. Project 2: Cloud Run, Artifact Registry, Vertex registry and its model files bucket (ADR-030), Gemini. Never put a resource in the wrong project.
- Budget is $50 credits per member, not pooled. The VM (about $31 a month running, about $3 stopped) runs 24/7 only for the live run and for tests, and is stopped otherwise (ADR-029).
- Never run `terraform apply` or `terraform destroy`. Before any change that creates a resource, print its running cost and the command that tears it down.

## Deadlines that drive priority
Deployment Dec 6. Expo requirements Dec 7, 11:59am. Expo Dec 8.

## Commands
Commands live in the READMEs and the runbook, not here. Use only commands documented there; do not guess. Add a new command to its component README or runbook section, in the PR that introduces it. Rules that hold for every command:
- Tests and lint are what CI runs: `docker compose run --rm --build test` (Python 3.12, the DAG tests included), the contract tests `docker compose run --rm test docs/contracts/tests --no-cov`, and `ruff check .` with ruff 0.17.2. The required check is the `ci` job, which passes only when every other job in `ci.yml` passed.
- Coverage: 70 percent line coverage per component folder, each on its own (`.coveragerc`, `run_tests.sh`). A new component copies `.github/component-template/`, adds a caller job of `.github/workflows/component-ci.yml` to `ci.yml` and adds that job to the `ci` job's `needs`; the bar is fixed at 70 in `component-ci.yml`. See the runbook's CI section.
- A new package in `Data-Pipeline/requirements.txt` takes the Airflow 3.3.2 constraints file's version; with none there, pin the version the tests passed on. A package in both `Data-Pipeline/requirements.txt` and `Model-Development/requirements.txt` keeps the same pin (`test_steps_requirements.py` checks).
- Never a bare `dvc pull` for model work (about 2.26 GB): pull only the two data files the model workflow reads. In DAGs and scripts use `python Data-Pipeline/scripts/dvc_push.py`, never bare `dvc push`.
- `Data-Pipeline/dvc.yaml` is generated: after changing a step run `python Data-Pipeline/scripts/dvc_stages.py`.
- Change a package of the training image only by the runbook's "Training image package lock (#111)", in the same PR as the `requirements.txt` change.
- Validate the compose file with `docker compose config --quiet` (no output means valid).
- Pause `daily_pipeline` before `airflow tasks test` or `airflow dags test` on it, or the scheduler also runs the test's temporary run.

| Task | Where |
|---|---|
| Local Airflow stack, hello DAG, venv, tests, lint | `README.md` "Local Airflow stack"; `docs/runbook.md` "Local Airflow stack (Docker Compose)", "Tests", "CI" |
| Run the DAG, a slice, one task | `Data-Pipeline/README.md` "Run it in Airflow", "Small run: one year"; `docs/runbook.md` "Run the data pipeline DAG" |
| Data step scripts (parse, map, spatial join, counts, labels, features, validation, stats) | `Data-Pipeline/README.md` "Run it step by step", "Code structure"; `docs/runbook.md` "Daily counts", "Surge labels and features (#16)", "Schema and statistics (every run)" |
| Downloads (311, weather, tides, alerts, static layers) | `Data-Pipeline/README.md` "Get the raw data"; `docs/runbook.md` "311 ingestion (issue #6)", "Weather and tide downloads", "Weather download (Open-Meteo, issue #8)", "Static layers" |
| Analyses behind ADRs (baselines, after-switch, units) | `docs/runbook.md` "Surge baseline re-check (ADR-005)", "After-switch policy (ADR-021)", "Spatial join (311 requests to tracts and neighborhoods)" |
| Bias slicing and mitigation | `Data-Pipeline/README.md` "Bias detection and mitigation"; `docs/runbook.md` "Bias slicing (#32)", "Bias mitigation (#33, ADR-025)" |
| DVC pull, repro, push, data versions | `README.md` "Versioned data (DVC)"; `docs/runbook.md` "Versioned data (DVC)", "Stages (#18, ADR-024)", "Data versions (#40)" |
| Pipeline alerts | `docs/runbook.md` "Pipeline alerts (Slack)" |
| Train, tune, select, bias check, sensitivity | `Model-Development/README.md` "Training core: candidate models, tracking, selection, hold-out results (M4)", "Hyperparameter tuning with Optuna (M6)", "Model bias check (M7)", "Sensitivity analysis: SHAP and hyperparameter importance (M10)"; `docs/runbook.md` "Training interface (#106, ADR-027)", "Model bias check (#110, ADR-033)", "Sensitivity analysis (#113)" |
| Training stage in DVC, tuning runs | `docs/runbook.md` "Training stage (DVC, #107)", "Hyperparameter tuning (#109)" |
| Gates, registry push, champion, rollback | `Model-Development/README.md` "Model registry: push, champion and rollback (M8)"; `docs/runbook.md` "Model registry (#111, ADR-006)", "dvc.lock check (#111)" |
| Training image, workflow steps, local chain, Slack notice | `Model-Development/README.md` "Run it without our cloud", "Model CI/CD pipeline and training image"; `docs/runbook.md` "Model CI/CD pipeline (#123)", "A Slack alert did not arrive" |
| MLflow server and local store | `Model-Development/README.md` "MLflow tracking server (M1)"; `docs/runbook.md` "MLflow tracking server (#104)", "MLflow tracking (#107)" |
| Terraform, VM power, Airflow on the VM | `docs/runbook.md` "Project 1 infrastructure (Terraform)", "Project 2 infrastructure (Terraform)", "Stop the VM when idle", "Airflow on the VM"; `Deployment/terraform/README.md` "Testing the .env render" |

Human only, never run by Claude Code: `terraform apply` and `destroy`, `gcloud auth application-default login`, the project 2 bootstrap (ADR-030 "Bootstrap"), adding or rotating a secret value, and filling `.env` keys. The VM's `.env` is rendered at every boot from Secret Manager (ADR-034); nobody edits it. Steps: runbook "Adding a secret value", "API keys in .env", "Secrets on the VM (#64, ADR-034)", "Apply (human only)".
