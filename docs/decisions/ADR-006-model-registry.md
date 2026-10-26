# ADR-006: Vertex AI Model Registry is the only model registry; MLflow tracks experiments, Cloud Run serves

- Status: proposed (Kirtan's approval requested on the PR)
- Date: 2026-10-26
- Owner: piyush1420
- Phase: setup

## Context

The spec's loop retrains automatically and promotes a new model only if it beats the current one, with automatic rollback (spec, drift story). At any moment that needs one answer to three questions: which model versions exist, which one serves, and which one to fall back to. Scoping weighed MLflow, which has its own model registry, against the Vertex AI Model Registry, and cut MLflow as a model registry (spec, out of scope). Two registries would give two answers that can drift apart, two sets of access to manage, and two things to keep alive through the 14-day unattended run. ADR-001 keeps one model registry, and the spec's revision log records the choice on 2026-10-04: Vertex AI as the only registry, MLflow for tracking, because of tool sprawl. This ADR records that choice and sets how a model version moves from training to serving and back.

Constraints from accepted ADRs and the spec:

- **Two projects (ADR-009).** Project 1 (Prit): the Airflow VM, Cloud Storage, monitoring and Secret Manager. Project 2 (Kirtan): Cloud Run, Artifact Registry, the Vertex AI Model Registry and Gemini. Each service sits in the same project as its main caller.
- **MLflow lives on the VM.** MLflow tracking runs on the Airflow VM in project 1 (spec, architecture) and shares its 4 GB with Airflow (ADR-010). Like every UI on the VM, it binds `127.0.0.1` and is reached only through an SSH tunnel (ADR-016).
- **No keys, no secrets in the repo.** The VM uses its attached service account, and no key file is created (ADR-015). Nothing secret goes in the public repo (ADR-012).
- **Time-based splits.** Model choice uses the 2024 validation year, and the 2025 to mid-2026 test window is kept for the final comparison (ADR-003).
- **Serving is on Cloud Run.** The forecast API, agent service and dashboard run there (spec, stack), and its revisions scale to zero (ADR-009).

What the registry offers, from Google's documentation (checked 2026-10-26). Google's pages now call the product "Model Registry on Gemini Enterprise Agent Platform" ([introduction](https://docs.cloud.google.com/vertex-ai/docs/model-registry/introduction)), at addresses still under `/vertex-ai/`. This ADR keeps the team's name, Vertex AI Model Registry, as in the spec and ADR-009.

- **Versions.** A model holds versions, and a new model can be imported "as a new version of an existing model" ([versioning](https://docs.cloud.google.com/vertex-ai/docs/model-registry/versioning)). Each version has its own description and labels.
- **Aliases.** An alias "can only be assigned to a single version, by model, at a time", and aliases "can be moved from one model version to another". Assigning an alias that another version holds removes it from that version. "One version of a model is required to carry the default alias at all times", and when that version is deleted, the alias "is automatically assigned to the next most recent version" ([aliases](https://docs.cloud.google.com/vertex-ai/docs/model-registry/model-alias)).
- **Model files.** They are kept in a Cloud Storage bucket whose region "matches the locational endpoint you're using", and a bucket in another project needs read access granted. Every imported model is associated with a serving container; prebuilt containers cover TensorFlow, PyTorch, scikit-learn and XGBoost ([import](https://docs.cloud.google.com/vertex-ai/docs/model-registry/import-model)).
- **Cost.** "There is no cost associated with having your models in the Model Registry. Cost is only incurred when you deploy the model to an endpoint or perform a batch prediction on the model." Online prediction is priced per node hour: "A node hour represents the time a virtual machine spends running your prediction job or waiting in an active state (an endpoint with one or more models deployed) to handle prediction or explanation requests" ([pricing](https://cloud.google.com/vertex-ai/pricing)).

What Cloud Run offers, from its documentation (checked 2026-10-26):

- **Revisions do not change.** "You cannot modify a revision after its creation." A revision is its container images plus configuration settings, such as environment variables ([resource model](https://docs.cloud.google.com/run/docs/resource-model)).
- **Testing before traffic.** A revision can be deployed with no traffic and a tag: "The tag lets you directly test the new revision at a specific URL, without serving traffic."
- **Rollback.** "Rolling back traffic to a revision updates the routing configuration immediately; Cloud Run does not re-run deployment health checks or verify external dependencies before directing traffic to the revision" ([rollouts and rollbacks](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration)).

Nothing here exists yet: `Deployment/terraform` covers project 1 only. The registry's model, the MLflow server and CI/CD for models are built in Model Development (spec, Nov 9 to 15: training CI/CD, registry, rollback), on a project 2 that has to be set up first (ADR-009).

## Decision

**The Vertex AI Model Registry in project 2 is the only model registry.** A model version exists for the system only once it is registered there, and its `production` alias records the version that serves.

**MLflow on the VM in project 1 tracks experiments and agent traces, and nothing else.** Every training and tuning run logs its parameters, metrics, data version and artifacts to MLflow. The response agent and critic log their traces there (spec, monitoring: MLflow tracing for the agents). MLflow's own model registry is not used: no registered models, stages or aliases in MLflow. Gemini, which the agent and critic call, is Google's hosted model and is not registered; the agents' prompts live in Git and their traces in MLflow.

**Cloud Run serves the forecast; there is no Vertex AI endpoint and no batch prediction.** A forecast API revision on Cloud Run loads the files of one registered version. The registry only records versions, so it adds no charge, while an endpoint bills for every node hour a model stays deployed, waiting time included.

What each service keeps:

| Service | Project | Keeps | Does not keep |
|---|---|---|---|
| MLflow tracking | 1 (VM) | Every run: parameters, metrics, plots, data version; agent traces | Which version serves; no registered models |
| Vertex AI Model Registry | 2 | One record per candidate version: where its files are and their checksum, its MLflow run ID, Git commit, DVC data version, evaluation metrics and gate result; the aliases | The files themselves; runs that never became candidates |
| Cloud Storage | 1 or 2, open (see Consequences) | The files of each registered version, at a path no later version overwrites | Version history beyond the files |
| Artifact Registry | 2 | The serving images (ADR-009) | Model versions |
| Cloud Run | 2 | The forecast API revisions, each set to one version ID, and the traffic split between them | Which versions exist |

### How a version moves

The surge forecast is one model in the registry. Every new version takes the same path, whether it comes from a push (spec, architecture) or from the retraining trigger (spec, drift story; ADR-007, still to be written in #27):

1. **Train and evaluate.** The run logs to MLflow. The evaluation uses the ADR-003 splits and the bias gate: the worst-group recall gap, computed with Fairlearn (ADR-008). The margin over the seasonal baseline and the fairness threshold are the ones the spec freezes before Model Development; this ADR sets neither.
2. **Register.** A run that passes is imported as a new version of the surge forecast model, with its files at a new path, and gets the alias `candidate`. Its version description and labels carry the MLflow run ID, the Git commit, the DVC data version, the evaluation metrics and gate result, and a checksum of its files, so every registered version traces back to its run, its data and the exact files it serves.
3. **Deploy without traffic.** CI/CD deploys a Cloud Run revision of the forecast API set to the candidate's version ID, with no traffic and a tag, and tests it at the tag's URL.
4. **Promote.** Only if the candidate beats the version that holds `production` on the same comparison, passes the bias gate and passes the revision's health check. What "beats" means (the metric, the window and any margin) is set in Model Development, not here. The job works on its own version ID, never on whatever holds `candidate` by then, and only one promotion runs at a time. It captures the version ID that holds `production` and records it with the promotion; a rollback restores that captured ID, not whatever `previous` names by then. Then `previous` moves to the captured version, `production` moves to the candidate, and Cloud Run sends all traffic to the candidate's revision. If one of these moves fails, the same job puts the others back. If the aliases and the serving revision still disagree, for example while the Vertex AI API is down, the revision Cloud Run serves is the truth: forecasts record its version ID, and the aliases are reconciled to it before the next promotion.
5. **Roll back.** If the deployment job's health check fails after the switch, Cloud Run sends all traffic back to the revision of the captured version, and `production` moves back to it. If that revision is gone or does not start, a new revision is deployed from that version's retained files and its known-good image, and takes the traffic only after its health check passes. If neither path works, the job fails with an alert and does not report a rollback. A rollback for worse graded accuracy or fairness uses the same moves; when monitoring (ADR-008) calls for one is set with the alert thresholds, which are still open. After a rollback, `production` and `previous` name the same version until the next promotion, and the failed version keeps only `candidate` until the next candidate takes it.

**The first version** has no `production` to beat and no earlier version or revision to fall back to, so `previous` stays unset. It is promoted on the checks that need no earlier version (the margin over the seasonal baseline, the bias gate and the health check), and its revision must pass the health check before it serves forecasts. If that deployment fails, there is no earlier model to restore. Fixing the deployment and deploying the same approved version again may be enough; a new version goes through the same path only when the model itself is at fault.

### Aliases and Cloud Run traffic

The registry's aliases and Cloud Run's traffic are two different levers, and only one of them changes what serves:

| Question | Registry aliases (`candidate`, `production`, `previous`) | Cloud Run traffic |
|---|---|---|
| Where it lives | Vertex AI Model Registry, project 2 | The forecast API service on Cloud Run, project 2 |
| What it says | Which version the team approved, and which one to fall back to | Which revision answers requests |
| What moves it | The promotion and rollback steps above | The same steps |
| Does moving it change what serves | No, never by itself | Yes, as soon as the routing changes |

Each revision is set to a version ID, not an alias, and a revision cannot change after it is created. If a revision loaded its model by alias instead, the instances Cloud Run adds as requests come in, including the first one after scaling to zero, would load whatever the alias named at that moment: one revision could serve two models, and sending traffic back to an old revision would not bring its old model back. With the version ID fixed in the revision, traffic decides what serves and the aliases record it.

`production` and `previous` are custom aliases, not `default`. Deleting a version moves `default` by itself, and the serving version must change only through a promotion or a rollback.

This ADR does not choose the forecast API's framework (open in CLAUDE.md) or the Cloud Run base image (ADR-013 leaves it to the serving ADR).

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Vertex AI Model Registry only, MLflow for runs and agent traces, Cloud Run serving (chosen)** | Registry: no charge while no model is deployed to an endpoint or used for batch prediction (Google's pricing page). Model files: Cloud Storage, billed per GB stored. MLflow: open source on the existing VM | Medium: access across the two projects, versions and aliases in CI/CD | Vertex AI API unavailable: no registering or alias moves, but the serving revision keeps serving its version, and Cloud Run traffic can still be sent back. Project 2 down: nothing serves (ADR-009). VM down: no new MLflow runs and no retraining, but the registry still names the serving version and the fallback |
| MLflow Model Registry only | $0 in licences; runs on the VM | Low to register | Against the spec, which cut MLflow as a model registry. The serving record sits on the VM in project 1, reachable only through an SSH tunnel (ADR-016). Cloud Run and CI/CD in project 2 would need a way into the VM, and while the VM is down nobody can tell which version serves or roll back |
| Both registries, kept in sync | As chosen, plus sync code | High: two writes per version | The two records drift apart, and during an incident they give different answers to "which model serves" |
| No registry: model files in Cloud Storage plus a pointer file | Cloud Storage only | Low at first | A hand-built registry with no aliases, history or audit trail; a bad pointer write changes what serves and leaves no trace |
| Vertex AI Model Registry plus Vertex AI endpoints for serving | Every node hour a model stays deployed, waiting time included (Google's pricing page) | Medium | Serving moves off Cloud Run, against the spec; an endpoint left deployed bills around the clock against project 2's $50 |

## Consequences

- **Project 2 comes first.** Registering, promoting and deploying all need project 2, as ADR-009 already says for Cloud Run, CI/CD and the Nov 16 live run. Its Terraform enables the Vertex AI API and the access below, and creates the bucket for model files if that bucket is in project 2. Model versions and aliases are data the pipeline's code writes through the Vertex AI API, like objects in a bucket, not infrastructure: Terraform does not hold them, and nobody creates them by hand in the console.
- **Access across projects needs both owners.** Whatever registers or promotes needs a role in project 2 that can import model versions and move aliases: CI/CD for pushes, and also the VM's service account (project 1) if retraining registers from the VM, which ADR-007 decides. If the forecast API loads the model files at run time, its service account needs read access to them, across projects if they stay in project 1's bucket. As with the monitoring scope in ADR-008, grants across the projects need Prit (project 1) and Kirtan (project 2). The VM's path uses its attached service account and no key file (ADR-015); the deployment work chooses how CI/CD authenticates.
- **Where model files live is still open.** Google requires the bucket to be in the registry's region. There are two options: project 1's data bucket, granted to project 2, or a new bucket in project 2. ADR-009 lists Cloud Storage under project 1, but it also puts each service in the same project as its main caller, here the forecast API in project 2, so the choice needs ADR-009's owner. Project 1's data bucket is in us-east1 (ADR-015, `Deployment/terraform/storage.tf`), so choosing it means importing models into a us-east1 registry and granting project 2's services read access across projects. Serving needs project 1 at run time only if Cloud Run loads the files from that bucket rather than having the model packaged into its deployed image; that packaging choice is left open. The choice of bucket and the registry's region are made with project 2's Terraform. Either way each version gets its own path that is never overwritten: the data bucket deletes the old copy of an overwritten object 30 days later (#48), which would leave a rollback target without its files.
- **MLflow cannot be reached from outside the VM.** It binds `127.0.0.1` (ADR-016), so GitHub Actions runners and the agent service on Cloud Run cannot log to it directly. How their runs and traces reach MLflow is decided in Model Development; this ADR opens no port. Two examples: CI starts the training on the VM, or runs log locally and are imported on the VM.
- **A serving container per version.** Every imported version is associated with a container. scikit-learn (logistic regression) and XGBoost have prebuilt ones; LightGBM does not. The forecast API image in Artifact Registry can be named as every version's container. Vertex AI never runs it, since there is no endpoint.
- **A rollback needs the old version intact.** Cloud Run sends traffic back without re-running health checks, so the current `production` version and the version a rollback would restore keep their model files, container images and deployment metadata while they are eligible for rollback. This ADR does not count on Cloud Run keeping every old revision, which is why step 5 can redeploy from those files and that image. Cleaning up older versions, revisions, images and files is an implementation policy.
- **Traceability.** Every registered version names its MLflow run and also carries its commit, data version, metrics and file checksum itself, so it stays traceable even if MLflow's records on the VM are lost (ADR-016: the VM's boot disk does not survive a replacement). Every forecast records the version ID that made it. The grading job and the retraining controller can then compare versions, and a rollback can be checked against the graded results.
- **The retraining controller.** If its learned policy is released (spec, Nov 19 to 25), it is registered under the same rules as a second model. The drift-threshold rule is a rule, not a trained model (spec, stack), so nothing is registered for it.
- **Costs to watch.** The registry adds no charge while nothing is deployed to an endpoint or used for batch prediction. Model files add Cloud Storage per GB; images add Artifact Registry storage in project 2 (ADR-022). Serving is Cloud Run's per-request cost in project 2 (ADR-009). MLflow's runs and traces use the VM's memory and disk (ADR-010 revisits at 80% memory).

## Revisit when

- Google starts charging for models kept in the registry, or changes the alias rules (one version per alias, aliases that can move).
- Serving moves off Cloud Run, or the team wants Vertex AI endpoints or batch prediction.
- MLflow's runs and traces no longer fit in the VM's memory or disk next to Airflow (ADR-010).
- Model Development finds no way for CI runs and agent traces to reach MLflow within ADR-016.
- Project 2 cannot carry the registry, or one member's credits have to carry both projects (ADR-009).
- ADR-007 sets how the retraining trigger runs, or the team sets the alert thresholds that can call a rollback.
