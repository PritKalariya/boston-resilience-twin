# ADR-037: Deployment architecture: a Cloud Run model service, a monitor workflow in GitHub Actions, a drift-threshold rule, and per-component CI

- Status: accepted 2026-11-23
- Date: 2026-11-23
- Owner: PritKalariya
- Phase: deploy
- Amends ADR-007: the learned retraining controller (contextual bandit, Q-learning stretch) is cut; the drift-threshold rule stays, with thresholds calibrated on a replay and a cooldown (decision 6). Only ADR-007's status line changes, to point here.
- Amends ADR-008: MLflow tracing for the agents is cut (decision 9). ADR-008's two monitoring tools stand. Only ADR-008's status line changes, to point here.

## Context

The Deployment plan (spec revision log, 2026-11-19) fixed the shape of serving, data placement, monitoring and retraining in three weeks, skeleton first. #159 freezes the formats the components share (`docs/contracts/`); this ADR records the architecture those formats assume, so each owner can build against it. Each decision below is short: the choice, what it was chosen against, why (the revision log's reasons), and where it breaks.

## Decision

### 1. Serving: a Cloud Run model service with the model baked into its image

The model service (FastAPI: `/predict`, `/forecasts/{date}`, and the map page) runs on Cloud Run in project 2 with the registered model in its image. A promotion deploys a revision with no traffic, smoke-tests it and moves the traffic. Rollback is a traffic move to the previous revision.

- Chosen against: a Vertex AI endpoint, and batch-only scoring on the VM.
- Why: a Vertex endpoint costs about 70 dollars a month, and ADR-030 provisions none. Batch scoring on the VM does not fit the VM memory budget (ADR-031), and the model libraries are outside the Airflow constraints (ADR-022).
- Breaks when: a cold start makes the evening run's `/predict` call time out (set minimum instances to 1 and price it), or the image grows past what a build and a rollback can move in minutes.

### 2. Data placement: forecasts, manifests and monitoring outputs in the project 1 data bucket

Forecasts, data manifests and monitoring outputs live in `gs://boston-resilience-twin-data` (project 1), beside the DVC remote. The model service, the agent service and Gemini on Vertex AI are in project 2 (ADR-009).

- Chosen against: a second bucket in project 2 for serving data.
- Why: the spec's revision log places them there, and the VM already writes to this bucket (decision 8).
- Breaks when: project 2 services read the bucket at a volume that makes cross-project egress or latency matter, or when the bucket starts holding non-public data (decision 8).

### 3. The live data version is the sha256 of the evening's manifest

`data_version` is `sha256:` plus the sha256 of the bytes of `manifests/<as_of>.json`, in the canonical form of `docs/contracts/data-manifest.md`.

- Chosen against: the `dvc.lock` hash, which is the training data version.
- Why: a live run does not commit a `dvc.lock`, so the lock hash cannot say which data an evening's forecast used.
- Breaks when: two runs produce the same manifest bytes from different data (the manifest then fails to name its inputs), or the canonical form changes without a version bump.

### 4. Monitoring runs in a scheduled GitHub workflow

Grading, Evidently drift, the fairness checks and the retrain trigger run in a scheduled GitHub Actions workflow (#163, #169), in `Monitoring/`'s own image.

- Chosen against: running them on the VM in Airflow.
- Why: the VM memory budget (ADR-031); Evidently is outside the Airflow constraints (ADR-022); and it runs even when the VM is stopped.
- The IAM cost, stated plainly: the spec's "no new IAM for the VM or CI" is not right for CI. `ci-writer` gets `roles/storage.objectUser` on the data bucket, conditioned to the `monitoring/` prefix (#163), and `roles/monitoring.metricWriter` on project 1 (#169). Both are new grants for an identity that today writes only in project 2.
- Breaks when: a scheduled run is skipped or starts late, so `forecast_freshness_hours` rises and the freshness alert fires; or the workflow's runtime outgrows the job timeout.

### 5. A polling trigger: the monitor workflow dispatches the retrain

On its schedule the monitor workflow reads `monitoring/decisions/latest.json` and, when it says retrain, dispatches `model.yml`.

- Chosen against: the VM or a bucket event calling GitHub.
- Why: either needs a GitHub token on the VM or new event infrastructure; polling needs neither.
- Breaks when: the delay matters. A decision waits up to a day for the next scheduled run, and GitHub's scheduled runs can start late. Revisit if a retrain must follow a decision within hours.

### 6. A fairness-aware drift-threshold rule replaces the learned controller

Retraining is triggered by the drift-threshold rule (decay in 30-day F1, drift lasting several days, or the poverty-group recall gap above 0.15), with thresholds calibrated on a replay of held-out history (#170) and frozen before go-live, and a cooldown (at most one triggered retrain a day, plus a manual force). Promotion is unchanged: a retrained model replaces the current one only if it beats it, with automatic rollback. This amends ADR-007.

- Chosen against: the contextual bandit and Q-learning stretch of ADR-007, never retraining, and a fixed weekly retrain.
- Why: three weeks, skeleton first, and the controller is not graded (spec revision log, 2026-11-19).
- Breaks when: on the replay the rule fires on most days or never (ADR-007's own signal), or live data is too sparse to move a threshold, in which case the replay carries the demonstration.

### 7. Per-component CI images

Each component has its own image (a `runtime` and a `test` stage), its own job in `ci.yml` that calls `.github/workflows/component-ci.yml`, and its own fixed 70 percent line-coverage bar, measured in the image with no network. `.github/component-template/` runs on every pull request and proves the pattern. The `ci` aggregator job fails unless every other job succeeded.

- Chosen against: adding every component's packages to the Airflow image (ADR-022's constraints), and one shared image.
- Why: the model service, Evidently and the agent have libraries outside the Airflow constraints (ADR-022); a shared image would hold all of them at once. The test stage adds only test packages, so tests run on the packages that ship.
- Breaks when: building many images makes the 15-minute job limit or the runner minutes a problem, or a component needs a package the pinned base image cannot install.

### 8. IAM the placement needs

Each line is the identity, the role, the scope and the issue. The bucket is in project 1; the three project 2 identities reach it across projects (ADR-009). These grants live in project 1's Terraform (`Deployment/terraform/`), applied by Prit.

| Identity | Role | Scope | Issue |
|---|---|---|---|
| `airflow-vm-sa` (project 1) | `roles/storage.objectAdmin` (exists, `Deployment/terraform/iam.tf`) | `gs://boston-resilience-twin-data`: writes forecasts and manifests | #162 |
| `ci-writer` | `roles/storage.objectUser` | the data bucket, conditioned to `monitoring/` | #163 |
| `ci-writer` | `roles/monitoring.metricWriter` | project 1 | #169 |
| model service runtime service account (project 2) | `roles/storage.objectViewer` | the data bucket: the map's reads of forecasts and briefs | #161 |
| agent service runtime service account (project 2) | `roles/storage.objectViewer` | the data bucket: reads the forecast file, monitoring files and the forecast rows file | #173 |
| agent service runtime service account (project 2) | `roles/storage.objectUser`, conditioned to objects whose name starts with `forecasts/` and ends with `.brief.json` | the data bucket: creates the brief and overwrites it on a rerun | #173 |

The critic runs inside the agent service and uses its identity, so it has no grant of its own (#174). If it ever runs elsewhere, that identity needs its own `roles/storage.objectViewer` on the bucket.

- Chosen against: reads scoped to the prefixes each component uses.
- Why bucket-wide reads: everything in the bucket is derived from public data (311, weather, tides, census); list permission cannot be scoped by prefix; and the forecast rows file sits under `dvc/` anyway, beside the training data.
- Breaks when: the bucket starts holding non-public data. Then the reads get scoped and the forecast rows file moves to its own prefix.

### 9. No MLflow tracing for the agents

The agent service and the critic write no MLflow traces. ADR-008 kept MLflow for "experiment tracking and agent traces"; the experiment tracking stays, the agent traces are cut.

- Chosen against: MLflow tracing for the agents (ADR-008).
- Why: MLflow runs only under a compose profile that is off by default (ADR-031, #135), so it is a local tool, and no MLflow server is deployed in the cloud. The agent service on Cloud Run has nowhere to send traces. The brief records every claim's source, the critic's result is kept beside it, and the service's runs are in Cloud Logging.
- Breaks when: an MLflow server (or another trace store) is deployed in the cloud, or the team needs per-call prompt, latency or token traces that Cloud Logging does not hold.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Vertex AI endpoint for serving | About 70 dollars a month | Medium | Over budget; ADR-030 has none |
| Batch-only scoring on the VM | None extra | Medium | Memory budget (ADR-031) and the Airflow constraints (ADR-022) |
| Monitoring in Airflow on the VM | None extra | Low | Evidently outside the constraints; does not run when the VM is stopped |
| A VM or bucket event calls GitHub | A token on the VM, or event infrastructure | High | A new secret on the VM, or a new moving part |
| Learned retraining controller | Replay training on the VM | High | Unproven policy, not graded, three weeks (ADR-007) |
| One shared CI image | None extra | Low | One component's pin breaks another's tests |
| **This ADR** | Cloud Run (scales to zero), two new CI grants | Medium | Each decision's "breaks when" |

## Consequences

- The serving, agent and monitoring components build against `docs/contracts/` and this placement; a change to either needs its own PR.
- CI has a second kind of job. A new component copies the template, adds its caller job and adds its id to `needs` of `ci`; `Model-Development/tests/test_ci_workflows.py` fails when it does not.
- The repo admin makes `ci` the required status check after this merges, in place of `Lint and test (Python 3.12)` (runbook, "CI"). Until then the old check stays required and `ci` only reports.
- `ci-writer` now writes to project 1 as well as project 2. The metrics it writes need `roles/monitoring.metricWriter` (`docs/contracts/metric-names.md`).
- Cost: Cloud Run and the new CI images fit the per-member budgets (spec, "Cost"); the new IAM grants are free. Teardown of the CI pattern: `git revert <merge sha>`; Terraform teardown belongs to the issues that create the resources.

## Revisit when

- A signal in a decision's "breaks when" appears: a cold-start timeout (1), a skipped or late monitor run (4, 5), the rule firing daily or never (6), or CI image builds near the job limit (7).
- A cloud trace store appears, or per-call agent traces are needed (9).
- #170's replay changes a threshold assumption, or the course asks for a learned controller again (6).
- The bucket starts holding non-public data (2, 8).
