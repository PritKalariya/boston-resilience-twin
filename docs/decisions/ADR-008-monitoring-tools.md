# ADR-008: Two monitoring tools, Cloud Monitoring and Evidently

- Status: accepted
- Date: 2026-10-04
- Owner: Kirtan10
- Phase: setup

## Context

The live system has two kinds of things to watch, and they fail in different ways.

- **The services.** The Airflow VM and its DAGs in project 1, and the forecast API, agent service and dashboard on Cloud Run in project 2 (ADR-009). If the VM stops or a DAG fails, no forecast goes out that evening.
- **The data and the model.** The spec's drift story: weather, request mix and volume compared with the training data, rolling 7- and 30-day F1, precision and recall on provisional and final grades, and the 30-day worst-group recall gap. These feed the alerts and the retraining trigger (drift-threshold rule first, the controller once it wins, ADR-007).

Scoping listed more tools than the team could run: Grafana, Prometheus and W&B Weave next to Cloud Monitoring and Evidently. Each is one more thing to install, secure and keep alive through the 14-day unattended run. The budget is $50 per member, not pooled, and the e2-medium VM has 4 GB of memory that Airflow already uses heavily (the full pipeline build peaked at 3.4 GiB in the scheduler). ADR-001 cut the list to two monitoring tools; this ADR records which two and what each covers.

## Decision

**Cloud Monitoring** (project 1) watches the services:

- VM health and uptime checks on the Cloud Run services, with Cloud Run's request, error and latency metrics.
- Alerting policies notify Slack and email.
- Pipeline task and check failures are not routed through it: the DAG's failure callback sends those itself (#34), since the containers' logs stay on the VM.

**Evidently** (open source, run as a pipeline task on the VM) watches the data and the model:

- Drift reports on weather, request mix and volume against the training reference. Request mix is measured on the storm groups after `docs/data/category-map.csv`, not on raw 311 categories, so renamed, merged and split categories (ADR-020, ADR-005) do not read as drift.
- Rolling accuracy on the graded forecasts: F1, precision and recall, with the worst-group recall gap reported next to them.
- Its results go to the same Slack and email alerts and to the retraining trigger.

**The worst-group recall gap is computed with Fairlearn**, not Evidently: `MetricFrame` over the 24 ADR-004 neighborhoods, the same code as the bias slicing in #32, run in the grading task. Evidently only reports and alerts on it, so there is one definition for the controller, the alerts and the README. Following ADR-004, the worst-group metric leaves out any neighborhood with no surges in the window (Harbor Islands, so far) and reports that it did.

**Not used:** Grafana, Prometheus, W&B Weave, and MLflow as a model registry (Vertex AI Model Registry is the only registry, ADR-006). MLflow stays for experiment tracking and agent traces, which the spec counts as tracking, not a third monitoring tool.

Alert thresholds are not set here. The spec sets them after week 1 of the live run. That covers when monitoring alerts, not the fairness target: the worst-group recall gap's threshold is frozen before Model Development (spec, metrics table) and is not reopened by the alert thresholds.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Cloud Monitoring plus Evidently (chosen)** | $0 in the term: Google system metrics are free and alerting is not charged before Sep 2027; Evidently is open source on the existing VM | Low for service alerts; medium for the drift and grading reports | Two places to look; drift checks stop while the VM is down, but Cloud Monitoring, outside the VM, still alerts on that |
| Prometheus and Grafana on the VM | $0 in licences | High: exporters, storage, dashboards, upkeep | Competes with Airflow for the 4 GB; when the VM goes down, monitoring goes with it and nothing alerts |
| Hosted Grafana Cloud or W&B Weave | Free tiers with limits | Medium: another account and key outside GCP | A third-party outage or tier limit; data leaves GCP; needs exporters to see GCP resources |
| Cloud Monitoring only, drift written by hand | $0 | High: rebuild drift tests and reports | Reinvents what Evidently gives; weaker drift reports for the team and graders |
| Evidently only | $0 | Low | No uptime or service alerts: a stopped VM or a failing Cloud Run revision goes unnoticed |

## Consequences

- Service alerts live outside the VM, so a dead VM still pages someone.
- Cloud Run metrics belong to project 2. To see both projects in one place, project 1's Cloud Monitoring metrics scope includes project 2; the monitoring issue sets that up through Terraform. Linking needs permissions in both projects, so both owners take part: Prit (project 1) and Kirtan (project 2), per ADR-009.
- The 311 migration is an expected drift source (spec, drift story), not an alarm. Besides measuring request mix on storm groups, the drift reference window is chosen with the switch dates in `docs/data/change-points.csv` in mind, so the retraining trigger does not fire on reclassification alone.
- Evidently joins the Airflow image (ADR-022) through `Data-Pipeline/requirements.txt` when the first report task lands, pinned like every other package.
- The Slack webhook and email credentials live in Secret Manager (ADR-015). Their writers are decided before the slots are added, as ADR-019 asks.
- Failed pipeline checks and tasks reach Slack and email through #34; drift and grading alerts use the same notifier, so there is one alert path to test.
- The dashboard and map libraries are still open and are not chosen here.

## Revisit when

- Cloud Monitoring starts charging for alerting (no sooner than Sep 2027) and the alert count makes it cost more than a few dollars a month.
- Evidently's reports no longer fit in the VM's memory next to the pipeline.
- The team needs a metric that neither tool can alert on.
