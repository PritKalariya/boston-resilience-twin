# Project Spec: Boston Resilience Twin

Working base for every later chat. Matches the scoping document submitted on Oct 4, 2026; the PDF has the full detail. Repo: https://github.com/PritKalariya/boston-resilience-twin

**Problem:** Before storms, freezes, and heat waves, Boston's 311 requests surge faster than crews can respond, and unevenly across neighborhoods. There is no public daily forecast of where demand will rise, and no check that such a forecast works equally well for every neighborhood.

**User:** City staff in emergency management, climate resilience, and operations. Advisory only: a person reviews every recommendation, and official warnings stay with the City and the NWS. Secondary user: public understanding.

**Solution:** Each evening, forecast for every neighborhood whether the next day brings a surge in storm-related 311 requests. Grade every forecast against the next day's reports (provisional the next evening, final after 48 hours), track accuracy and fairness, detect drift, and retrain automatically, promoting a new model only when it beats the current one. A single response agent plus a critic turns forecasts into briefs where every number is sourced, shown on a digital twin map. Headline innovation: a fairness-aware retraining controller (contextual bandit) that decides when and how to retrain, compared with three baselines on held-out history and reported either way.

**Out of scope** (cut during scoping; some sit in the stretch list):
- Multi-agent layer (coordinator plus 8 tools) and multilingual public messages
- Scenario explorer and the Q-learning controller (stretch)
- MBTA transit alerts, road work, street trees, cooling and warming centers, income-restricted housing
- Grafana, Prometheus, W&B Weave, and MLflow as a model registry
- Official warnings, and any individual-level data or outreach

## Data

| Item | Detail |
|---|---|
| Primary | Boston 311 Service Requests on Analyze Boston (PDDL): yearly legacy files back to 2011, plus the new-system DataStore. BARI archive on Harvard Dataverse (2010 onward) |
| Supporting | NWS forecasts and alerts; NOAA Logan history; NOAA tides (Boston Harbor gauge); Climate Ready Boston flood-risk and social vulnerability layers; ACS demographics; city population estimates; neighborhood and tract boundaries; Open-Meteo history and archived forecasts (2021 onward, CC BY 4.0, attribution required) |
| Access | All public. Free NOAA CDO token and Census key; NWS needs an identifying user agent. Keys in Secret Manager |
| Refresh | Daily forecast DAG every evening; event DAG every 10 to 15 minutes while an alert is active; static layers reloaded when the source updates |
| Split | Time-based, never shuffled: train 2015 to 2023, validate 2024, test 2025 to mid-2026, live from deployment. 2020 to 2021 flagged and down-weighted. A feature is used only if its history covers the training years. Observed weather in training makes backtests upper bounds; Open-Meteo archived forecasts replace it for validation and test |
| Target | Next-day surge flag, storm-related requests per neighborhood per day (working choice). Surge: more than k standard deviations above baseline (k 1.5 to 2) and at least m requests (m 3 to 5), compared with a top-10-percent rule. Frozen in #106 (M3) before training starts |
| Known issues | 311 migration through fall 2026: categories not one-to-one, new-system times carry a +00 suffix but are Boston local time (ADR-020), switch dates treated as change points. Sparse cells (77 percent empty in the first run). The first-run parser lost about a third of legacy rows; fixed and counts re-measured in phase 1 |
| Week-1 decisions | Done: modeling unit neighborhood x day (ADR-004, proposed until M7 accepts it), live baselines (ADR-005), alert history source (ADR-014) |

Pre-scoping check (Oct 3): 833,700 rows from Jan 2024 to early Oct 2026, about 830 a day (the city reports over 1,000). Storm groups are about 15 percent of requests. Snow days brought 4.9x snow and ice requests; heavy rain 2.5x flooding and 1.7x tree requests; heat only 1.13x total. Surges were 1.3 to 2.9 percent of cells.

## Metrics

| Layer | Metric | Target | Baseline |
|---|---|---|---|
| ML | Surge-class F1, precision, recall | Beat the baseline by at least 0.05 F1 on validation (frozen Oct 30) | Seasonal baseline |
| ML | Same metrics on storm, freeze, and heat days | Reported as a separate slice | Seasonal baseline |
| ML | Precision among each day's top-ranked neighborhoods | Reported daily, tracked over time | None set |
| Fairness | Worst-group recall gap (best minus worst recall) | Gate: at most 0.15 over the 2024 validation year between the 2 ACS poverty groups (median split, frozen), groups under 5 surges pooled (frozen Oct 30, groups set Nov 2). Live: 30-day windows, groups set at Deployment planning | Baseline analysis |
| Software | Forecast delivery | Every evening, no manual steps | None |
| Software | Live run | Length set at Deployment planning; monitoring and automatic retraining demonstrated | None |
| Software | Agent briefs | 100 percent of numbers sourced; ready within minutes of an alert | None |
| Outcome | Controller vs fixed schedules (accuracy, fairness gap, compute) | Compared on held-out history, reported either way | Never retrain, weekly retrain, drift-threshold rule |
| Cost | Cloud spend | Within 50 dollars per member; VM about 1 dollar a day while running, off except for tests and the live run | Course credits |

Metric chain: higher surge recall means fewer surges found only after reports arrive; higher top-ranked precision means fewer wasted crew preparations; together they move crew time from reacting to preparing. The fairness gap keeps that gain from going only to neighborhoods that already report often.

Known gap: no latency or throughput metric.

## Architecture

Airflow in Docker on an e2-medium VM runs the daily and event DAGs, saves raw pulls and versioned data to Cloud Storage (the DVC remote), and hosts the grading job and Evidently. MLflow tracking runs as a docker compose service on laptops and the VM. On every push, GitHub Actions tests, trains, validates, applies the bias gate, registers the model in Vertex AI Model Registry, builds images into Artifact Registry, and deploys a Cloud Run revision (forecast API, agent service, dashboard), with rollback if health checks fail. Cloud Run calls Gemini on Vertex AI for the response agent and critic. Cloud Monitoring and Evidently feed Slack and email alerts and the retraining trigger.

Hosting is split because credits don't pool. Project 1 (Prit): the VM, Cloud Storage, monitoring, and Secret Manager. Project 2 (Kirtan): Cloud Run, Artifact Registry, the Vertex registry, and Gemini. Diagram: Figure 3 in the scoping doc.

## Stack

| Component | Decided | Candidate or open |
|---|---|---|
| Orchestration | Airflow in Docker on an e2-medium VM; LocalExecutor with a Postgres 16 container; UIs by SSH tunnel only (ADR-016) | None |
| Data versioning and checks | DVC with a Cloud Storage remote, Great Expectations, Fairlearn slicing | None |
| Models | Seasonal baseline, logistic regression, LightGBM, XGBoost, Optuna, SHAP | None |
| Tracking and registry | MLflow for tracking, as a compose service; Vertex AI Model Registry as the only registry | None |
| Retraining controller | Contextual bandit trained on replayed history; drift-threshold rule until it wins | Q-learning (stretch) |
| Serving | Cloud Run: forecast API, agent service, dashboard | API framework (FastAPI is the candidate: typed request validation, auto-generated API docs, a light container for Cloud Run); dashboard and map libraries |
| Language model | Gemini on Vertex AI | Model tier; per-event cap value |
| CI/CD and IaC | GitHub Actions; Terraform | None |
| Monitoring | Cloud Monitoring and Evidently; MLflow tracing for the agents | Alert thresholds after week 1 |
| Notifications | Slack and email | None |
| Runtime | Python 3.12 (ADR-013), Docker Compose for local development | None |

## Team and owners

| Area | Owner |
|---|---|
| Data pipeline | Srikar |
| Fairness and data quality | Alekhya |
| Forecast model, plus the response agent and critic | Piyush |
| Retraining controller | Punith |
| Deployment | Prit |
| Monitoring, plus the map, dashboards, storm replay, and the live run | Kirtan |

## Milestones

| Dates | Focus | Main deliverables |
|---|---|---|
| Oct 5 to 11 | Setup and ingestion | Repo, environments, Airflow VM via Terraform, API access, 311 and weather ingestion, week-1 decisions |
| Oct 12 to 18 | Processing | Format mapping, spatial joins, daily counts, surge labels, validation, DVC |
| Oct 19 to 27 | Pipeline quality | Bias slicing, anomaly alerts, Gantt analysis, tests. **Data Pipeline due Oct 27 (moved from Oct 25), submitted** |
| Oct 28 to Nov 1 | Phase 2 prep | Checkpoint CP1, workflow redesign, Model Development planning |
| Nov 2 to 6 | Interfaces and first pieces | MLflow, training interface and seasonal baseline, data fixes, project 2 infra; VM startup script |
| Nov 9 to 12 | Models and CI/CD | Training core, tuning, bias, registry and rollback, CI/CD, sensitivity; VM run, memory cap, VM secrets. Code freeze Nov 12 |
| Nov 13 to 15 | Closing | Final README, fresh-clone test, submission. **Model Development due Nov 15** |
| Nov 16 to Dec 6 | Deployment | Serving, monitoring and drift-triggered retraining, controller, response layer, map and dashboards, live run, fresh-machine video; detailed at Deployment planning. **Deployment due Dec 6** |
| Dec 7 | Expo | Requirements due before noon |

The VM is stopped when idle and runs for tests and the live run; the run's length is set at Deployment planning. Lab Assignments 4 and 6 fall on the same days as Model Development (Nov 15) and Deployment (Dec 6).

## Drift story

- **Where it drifts:** seasons (snow and ice in winter, heat in summer), storm events, the 311 migration through fall 2026 (reclassification, new categories), and uneven reporting across neighborhoods.
- **How we detect it:** Evidently on weather, request mix, and volume compared with training data; rolling 7- and 30-day F1, precision, and recall on provisional and final grades; the 30-day worst-group recall gap.
- **What triggers retraining:** the drift-threshold rule retrains from day one of the live run. The learned controller replaces it only after beating that rule on replayed history. A safety rule always retrains past hard accuracy or fairness limits, and a new model is promoted only if it beats the current one, with automatic rollback.

## Open risks

1. **Live labels:** live data is mostly the new 311 format while baselines come from legacy years, so live "surges" may be reclassification. Settled by ADR-005.
2. **Sparse target:** settled in part: the unit is locked (ADR-004) and counts are re-measured; the surge rule is frozen in #106.
3. **Controller:** it may never beat the drift-threshold rule, and its reward is noisy with 1 to 3 percent positives. The doc reports the result either way.
4. **Feature history:** alert history is settled (ADR-014: Iowa Environmental Mesonet archive, 2015 onward). Archived forecasts still only start in 2021.
5. **Cost:** the e2-medium (about 31 dollars a month running: VM 24.46, disk 3, IP 3.65) plus Gemini calls must fit 50-dollar projects, and the second hosting member is Kirtan.
6. **Workload:** Nov 9 to 12 holds most Model Development code, with Lab 4 due Nov 15; monitoring moved to Deployment.
7. **Expo event date** is still unverified in the course facts.

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
