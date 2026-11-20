# Project Spec: Boston Resilience Twin

Working base for every later chat. Matches the scoping document submitted on Oct 4, 2026; the PDF has the full detail. Repo: https://github.com/PritKalariya/boston-resilience-twin

**Problem:** Before storms, freezes, and heat waves, Boston's 311 requests surge faster than crews can respond, and unevenly across neighborhoods. There is no public daily forecast of where demand will rise, and no check that such a forecast works equally well for every neighborhood.

**User:** City staff in emergency management, climate resilience, and operations. Advisory only: a person reviews every recommendation, and official warnings stay with the City and the NWS. Secondary user: public understanding.

**Solution:** Each evening, forecast for every neighborhood whether the next day brings a surge in storm-related 311 requests. Grade every forecast against the next day's reports (provisional the next evening, final after 48 hours), track accuracy and fairness, detect drift, and retrain automatically, promoting a new model only when it beats the current one. A single response agent plus a critic turns forecasts into briefs where every number is sourced, shown on a digital twin map. Headline innovation: fairness-aware retraining, a drift-threshold rule that also retrains when the recall gap between poverty groups widens, with its thresholds calibrated on a replay of held-out history.

**Out of scope** (cut during scoping; some sit in the stretch list):
- Multi-agent layer (coordinator plus 8 tools) and multilingual public messages
- Scenario explorer (stretch)
- The learned retraining controller (contextual bandit, with its Q-learning stretch) and the event DAG, cut at Deployment planning
- MBTA transit alerts, road work, street trees, cooling and warming centers, income-restricted housing
- Grafana, Prometheus, W&B Weave, and MLflow as a model registry
- Official warnings, and any individual-level data or outreach

## Data

| Item | Detail |
|---|---|
| Primary | Boston 311 Service Requests on Analyze Boston (PDDL): yearly legacy files back to 2011, plus the new-system DataStore. BARI archive on Harvard Dataverse (2010 onward) |
| Supporting | NWS forecasts and alerts; NOAA Logan history; NOAA tides (Boston Harbor gauge); Climate Ready Boston flood-risk and social vulnerability layers; ACS demographics; city population estimates; neighborhood and tract boundaries; Open-Meteo history and archived forecasts (2021 onward, CC BY 4.0, attribution required) |
| Access | All public. Free NOAA CDO token and Census key; NWS needs an identifying user agent. Keys in Secret Manager |
| Refresh | One evening run of the daily DAG, after the 18:00 cutoff; static layers reloaded when the source updates |
| Split | Time-based, never shuffled: train 2015 to 2023, validate 2024, test 2025 to mid-2026, live from deployment. 2020 to 2021 flagged and down-weighted. A feature is used only if its history covers the training years. Observed weather in training makes backtests upper bounds; Open-Meteo archived forecasts replace it for validation and test |
| Target | Next-day surge flag per neighborhood-day. Frozen in #106 before training as rule sd_k2_m5: a cell surges when its snow and ice or tree requests exceed the baseline mean plus 2 standard deviations and are at least 5. Flooding left the label: the new 311 system has no flooding category. Surge rate 3.42 percent in train, 4.79 in 2024, 5.11 in the test window. Recorded in ADR-027, still proposed at model-dev-v1 |
| Known issues | 311 migration through fall 2026: categories not one-to-one, new-system times carry a +00 suffix but are Boston local time (ADR-020), switch dates treated as change points. Sparse cells (77 percent empty in the first run). The first-run parser lost about a third of legacy rows; fixed and counts re-measured in phase 1 |
| Week-1 decisions | Done: modeling unit neighborhood x day (ADR-004, accepted; amended by ADR-027 to drop flooding from the label), live baselines (ADR-005), alert history source (ADR-014) |

Pre-scoping check (Oct 3): 833,700 rows from Jan 2024 to early Oct 2026, about 830 a day (the city reports over 1,000). Storm groups are about 15 percent of requests. Snow days brought 4.9x snow and ice requests; heavy rain 2.5x flooding and 1.7x tree requests; heat only 1.13x total. Surges were 1.3 to 2.9 percent of cells.

## Metrics

| Layer | Metric | Target | Baseline |
|---|---|---|---|
| ML | Surge-class F1, precision, recall | Beat the baseline by at least 0.05 F1 on validation (frozen Oct 30) | Seasonal baseline |
| ML | Same metrics on storm, freeze, and heat days | Reported as a separate slice | Seasonal baseline |
| ML | Precision among each day's top-ranked neighborhoods | Reported daily, tracked over time | None set |
| Fairness | Worst-group recall gap (best minus worst recall) | Gate: at most 0.15 over the 2024 validation year between the 2 ACS poverty groups (median split, frozen); a group under 5 surges is pooled, and anything unevaluable fails closed (frozen Oct 30, groups set Nov 2). Live: 30-day windows on the same 2 groups, reported as insufficient data below 5 positives per group | Baseline analysis |
| Software | Forecast delivery | Every evening, no manual steps | None |
| Software | Live run | From the first live evening (Nov 25) through the Expo (Dec 8); monitoring and automatic retraining demonstrated, with a replay where live data is too sparse | None |
| Software | Agent briefs | One daily brief after the evening forecast; 100 percent of numbers sourced, checked by the critic | None |
| Outcome | Retraining trigger on a replay of 2025-01-01 to 2026-06-30 | Fires a few times a season, not daily; thresholds frozen before go-live | None |
| Cost | Cloud spend | Within 50 dollars per member; VM about 1 dollar a day while running, off except for tests and the live run | Course credits |

Metric chain: higher surge recall means fewer surges found only after reports arrive; higher top-ranked precision means fewer wasted crew preparations; together they move crew time from reacting to preparing. The fairness gap keeps that gain from going only to neighborhoods that already report often.

Known gap: no latency or throughput metric.

## Architecture

Airflow in Docker on an e2-medium VM runs `daily_pipeline` each evening: it saves raw pulls and versioned data to Cloud Storage (the DVC remote), builds tomorrow's feature rows, calls the model service for the forecast, and writes the forecasts and a data manifest to the project 1 data bucket. A scheduled GitHub Actions workflow grades the forecasts, runs Evidently and the fairness checks, writes the monitoring outputs and Cloud Monitoring metrics, and dispatches a retrain when the fairness-aware threshold rule fires. MLflow tracking runs as a docker compose service on laptops only, never on the VM; CI logs to a local store inside the workflow artifact (90 days). On a model change or a dispatched retrain, GitHub Actions trains on the latest data, validates, applies the bias gate and registers the model in Vertex AI Model Registry; a promotion deploys a Cloud Run revision of the model service (FastAPI, the model baked into the image, also serving the map page) with no traffic, smoke-tests it and moves the traffic, rolling back on failure. The agent service on Cloud Run calls Gemini on Vertex AI for the daily brief, and a deterministic critic checks it. Cloud Monitoring dashboards and alert policies (project 1) and Slack carry the alerts.

Hosting is split because credits don't pool. Project 1 (Prit): the VM, Cloud Storage, monitoring, and Secret Manager. Project 2 (Kirtan): Cloud Run, Artifact Registry, the Vertex registry, and Gemini. Diagram: Figure 3 in the scoping doc.

## Stack

| Component | Decided | Candidate or open |
|---|---|---|
| Orchestration | Airflow in Docker on an e2-medium VM; LocalExecutor with a Postgres 16 container; UIs by SSH tunnel only (ADR-016) | None |
| Data versioning and checks | DVC with a Cloud Storage remote, Great Expectations, Fairlearn slicing | None |
| Models | Seasonal baseline, logistic regression, LightGBM, XGBoost, Optuna, SHAP | None |
| Tracking and registry | MLflow for tracking, a compose service on laptops only, never on the VM (ADR-031); Vertex AI Model Registry as the only registry | None |
| Retraining trigger | Drift-threshold rule, fairness-aware, thresholds calibrated on a replay, with a cooldown; the learned controller is cut | None |
| Serving | Cloud Run: the model service (FastAPI, the model baked into the image, also serving the map page) and the agent service | Map library |
| Language model | Gemini on Vertex AI for the agent; the critic is deterministic | Model tier; daily call cap |
| CI/CD and IaC | GitHub Actions; Terraform | None |
| Monitoring | Cloud Monitoring dashboards and alert policies; Evidently and grading in a scheduled GitHub workflow | Thresholds, calibrated on the replay |
| Notifications | Slack and email | None |
| Runtime | Python 3.12 (ADR-013), Docker Compose for local development | None |

## Team and owners

| Area | Owner |
|---|---|
| Contracts, serving and rollouts | Prit |
| Evening inputs, live data versions, retraining trigger and replay | Srikar |
| Monitoring, dashboards and alerts | Alekhya |
| Evening run on the VM, rolling split, response agent | Punith |
| CI and platform hygiene | Kirtan |
| Forecast map and brief critic | Piyush |

## Milestones

| Dates | Focus | Main deliverables |
|---|---|---|
| Oct 5 to 11 | Setup and ingestion | Repo, environments, Airflow VM via Terraform, API access, 311 and weather ingestion, week-1 decisions |
| Oct 12 to 18 | Processing | Format mapping, spatial joins, daily counts, surge labels, validation, DVC |
| Oct 19 to 27 | Pipeline quality | Bias slicing, anomaly alerts, Gantt analysis, tests. **Data Pipeline due Oct 27 (moved from Oct 25), submitted** |
| Oct 28 to Nov 1 | Phase 2 prep | Checkpoint CP1, workflow redesign, Model Development planning |
| Nov 2 to 6 | Interfaces and first pieces | MLflow, training interface and seasonal baseline, data fixes, project 2 infra; VM startup script |
| Nov 9 to 12 | Models and CI/CD | Training core, tuning, bias, registry and rollback, CI/CD, sensitivity; VM run, memory cap, VM secrets. Code freeze Nov 12 |
| Nov 13 to 15 | Closing | Final README and submission; the fresh-clone test was closed without a run. **Model Development due Nov 15, submitted** (tag model-dev-v1) |
| Nov 16 to 19 | Deployment planning | Checkpoint CP2, process changes, the Deployment plan and issues (#159 to #191) |
| Nov 20 to 25 | Skeleton | Contracts, evening inputs, the model service on Cloud Run, the evening run, the monitor workflow: one evening forecast served end to end |
| Nov 26 to Dec 1 | Hardening | Rollouts and redeploys, live data versions, rolling split, monitoring and drift, retraining trigger and replay, dashboards, map page, agent and critic; no due dates Nov 26 to 27 |
| Dec 2 to 5 | Closing | Final README, fresh-clone test, video, submission. **Deployment due Dec 6** |
| Dec 7 | Expo kit | Demo Day kit (name, logo, description, product video) due 11:59am |
| Dec 8 | Expo | MLOps Expo, 8:30am to 1:30pm |

The VM is stopped when idle and runs for tests and the live run, from the first live evening through the Expo on Dec 8. Lab Assignments 4 and 6 fall on the same days as Model Development (Nov 15) and Deployment (Dec 6).

## Drift story

- **Where it drifts:** seasons (snow and ice in winter, heat in summer), storm events, the 311 migration through fall 2026 (reclassification, new categories), and uneven reporting across neighborhoods.
- **How we detect it:** Evidently on the last 14 days of forecast rows against the same calendar weeks of earlier years; rolling 7- and 30-day F1, precision and recall on final grades, reported as insufficient data below 5 positives; the 30-day recall gap between the two poverty groups.
- **What triggers retraining:** a fairness-aware drift-threshold rule: decay in 30-day F1, drift lasting several days, or the poverty-group gap above 0.15, with thresholds calibrated on a replay and frozen before go-live; at most one triggered retrain a day, plus a manual force. A retrain uses the latest data on a rolling split, and a new model is promoted only if it beats the current one, with automatic rollback. The learned controller is cut.

## Open risks

1. **Live labels:** live data is mostly the new 311 format while baselines come from legacy years, so live "surges" may be reclassification. Settled by ADR-005.
2. **Sparse target:** settled: the unit is accepted (ADR-004) and the rule is frozen as sd_k2_m5 (ADR-027, still proposed), 3.42 percent surges in train. Four neighborhoods have under 5 validation surges, so the fairness gate compares the two poverty groups.
3. **Live data scarcity:** late-November surges are rare, so live metrics are often insufficient; the replay of held-out history demonstrates the trigger.
4. **Feature history:** alert history is settled (ADR-014: Iowa Environmental Mesonet archive, 2015 onward). Archived forecasts still only start in 2021.
5. **Cost:** the e2-medium (about 31 dollars a month running: VM 24.46, disk 3, IP 3.65) plus Gemini calls must fit 50-dollar projects, and the second hosting member is Kirtan.
6. **Workload:** hardening falls in Thanksgiving week, so the skeleton comes first and lands by Nov 25.
7. **Expo:** settled: Tue Dec 8, 8:30am to 1:30pm, with the Demo Day kit due Dec 7, 11:59am.

## Revision log

| Date | What changed | Why | Scope or idea? |
|---|---|---|---|
| 2026-10-04 | Agent layer cut from a coordinator with 8 tools to one response agent plus a critic; multilingual messages to stretch | Plan was about 780 hours against about 650 available | Scope |
| 2026-10-04 | Dropped MBTA alerts, road work, street trees, cooling centers, income-restricted housing | No training history, and scope | Scope |
| 2026-10-04 | Scenario explorer and Q-learning moved to stretch; replay narrowed to one past storm | Scope; Punith kept the one-storm replay as a backup demo | Scope |
| 2026-10-04 | Vertex AI as the only registry, MLflow for tracking; monitoring cut to Cloud Monitoring and Evidently | Tool sprawl | Scope |
| 2026-10-04 | Hosting split across two members' projects; VM created week 1, stopped in October, on 24/7 from Nov 1 | 50-dollar cap per member | Scope |
| 2026-10-04 | Monitoring moved before the 14-day run; controller given two weeks; drift-threshold rule retrains until the controller wins | Timeline gaps | Scope |
| 2026-10-04 | Controller success reframed as a comparison reported either way; cost-aware retraining (Cara) cited as prior art | Removed an overclaim | Scope |
| 2026-10-04 | Week-1 live-baseline decision, the no-history feature rule, and archived forecasts for validation and test added | Data validity | Scope |
| 2026-10-18 | Project 2 hosting member set: Kirtan | Host needed before Cloud Run and CI/CD work | Scope |
| 2026-10-20 | Modeling unit locked: neighborhood x day for the 24 tract-built neighborhoods (ADR-004, still proposed) | Week-1 decision | Scope |
| 2026-10-20 | Live baselines for the new 311 format decided (ADR-005) | Week-1 decision | Scope |
| 2026-10-22 | Known issues: new-system times are Boston local with a wrong +00 suffix, not UTC | ADR-020: resubmitted pairs differ by minutes, not 4 to 5 hours | Correction |
| 2026-10-27 | Open risk 4: alert history now has a source | ADR-014: the IEM archive covers every training year | Correction |
| 2026-10-27 | Data Pipeline deadline moved from Oct 25 to Oct 27 | Course change | Correction |
| 2026-10-28 | Self-relative 311 lags become the default model inputs; raw lags kept for comparison (ADR-025) | Bias mitigation in the data | Scope |
| 2026-10-30 | Retraining controller moved from Model Development to Deployment | Not in the Model Development rubric; Nov 9 to 15 was overloaded | Scope |
| 2026-10-30 | 14-day unattended run dropped as a fixed target; the VM stays stopped when idle; run length set at Deployment planning | Our own design, not the rubric's; Deployment needs monitoring and automatic retraining demonstrated | Scope |
| 2026-10-30 | MLflow runs as a docker compose service on laptops and the VM | Nobody waits on the VM; a grader's fresh clone can track runs | Scope |
| 2026-10-30 | Monitoring and drift-threshold retraining moved to Deployment | Matches the Deployment rubric; lightens Nov 9 to 15 | Scope |
| 2026-10-30 | Gates frozen: validation F1 at least 0.05 above the seasonal baseline; worst-group recall gap at most 0.15, groups under 5 surges merged | Fixed before training | Scope |
| 2026-10-30 | Model Development owners set; milestone timeline replanned | Load balance; replaces the original Oct 26 to Dec 2 plan | Scope |
| 2026-11-02 | Training fairness gate measured over the whole 2024 validation year between the 2 ACS poverty groups (the data pipeline's median split, frozen in `Model-Development/config/poverty_groups.csv`); neighborhoods and the other ACS families reported by #110, not gated; 30-day windows stay the live metric, groups set at Deployment planning. Champion replaced only on strictly greater validation F1, re-scored on the challenger's data version | 30-day windows give about 1 surge per neighborhood, neighborhood groups let one missed surge move recall by 0.20, and the pipeline has no 3-tier poverty definition | Scope |
| 2026-11-05 | Target frozen as sd_k2_m5 on the snow and ice and tree groups; flooding left the label (ADR-027, still proposed at model-dev-v1) | The new 311 system has no flooding category, so a live label could never contain a flooding surge | Scope |
| 2026-11-13 | #112 (model CI/CD) closed by #138 with the registry and champion as stubs and interim per-group counts; replacing them moved to #111 (registry, champion) and #110 (bias analysis) | #110 and #111 had no code at #112's due date; the step adapters were built for owners to plug in | Scope |
| 2026-11-17 | MLflow server runs on laptops only; it is not started on the VM, and whether it joins the VM moves to Deployment planning | CP2: the README and runbook never start it on the VM, and ADR-031's memory budget was measured without it, against the 2026-10-30 line | Correction |
| 2026-11-17 | Fairness gate: a group under 5 surges is pooled, not failed; ADR-004 is accepted | CP2: ADR-026 and the gate code pool small groups; ADR-004's status changed during Model Development | Correction |
| 2026-11-19 | Learned retraining controller cut, with its Q-learning stretch; retraining is a fairness-aware drift-threshold rule calibrated on a replay of held-out history | Three weeks, skeleton first, and not graded; the architecture ADR in #159 amends ADR-007 | Scope |
| 2026-11-19 | Event DAG cut; the daily DAG runs once each evening | Not in the Deployment plan or the rubric | Scope |
| 2026-11-19 | Serving: a Cloud Run model service with the model baked into its image, also serving the map page; dashboards in Cloud Monitoring; the critic is deterministic | Rubric fit; a Vertex endpoint costs about 70 dollars a month; effort | Scope |
| 2026-11-19 | Forecasts, data manifests and monitoring outputs live in project 1's bucket; a live run's data version is its manifest's sha256; grading, drift and the trigger run in a scheduled GitHub workflow | No new IAM for the VM or CI; the VM's memory budget; Evidently stays outside the Airflow constraints | Scope |
| 2026-11-19 | Retrains use a rolling split: the latest 365 days with final labels validate | With the fixed split every retrain reproduced the same model | Scope |
| 2026-11-19 | Deployment owners and dates set; the live run goes from Nov 25 to the Expo; the Expo confirmed for Tue Dec 8, the Demo Day kit due Dec 7 | Deployment planning; course facts | Scope |
