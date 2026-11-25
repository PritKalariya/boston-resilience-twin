# ADR-038: Serving on Cloud Run: two services from one image, a model that enters only at build, Terraform creates and a script deploys

- Status: proposed
- Date: 2026-11-25
- Owner: PritKalariya
- Phase: deploy
- Amends ADR-037 decision 8: the serving runtime account reads `forecasts/` only, not the whole data bucket (decision 4). Only ADR-037's status line changes, to point here.

## Context

ADR-037 decision 1 puts the model service on Cloud Run in project 2 with the registered model in its image, and rejects a Vertex endpoint (about 70 dollars a month, ADR-030 provisions none). It does not say how the service is shaped, who may call it, how the model gets into the image, how the services are created, or how a revision is deployed. #161 builds all of that in three PRs: the service (`Serving/`), its component CI, and this one, the Cloud Run Terraform, the deploy script and its workflow.

Two facts shape the choices. Cloud Run invoker IAM is per service, not per path, so `/predict` cannot be private on a service that also serves a public map. And unpickling a model runs arbitrary code, so the bytes that reach the image must be the bytes the registry recorded.

## Decision

### 1. Two Cloud Run services from one image, chosen by `SERVING_ROUTES`

`brt-model` (`SERVING_ROUTES=model`: `/health`, `/predict`) is not public: `roles/run.invoker` goes to `airflow-vm-sa` (project 1) only, and project admins reach it through `roles/run.admin` (ADR-036). `brt-web` (`SERVING_ROUTES=web`: `/health`, `/forecasts/{date}` and the map page, with `FORECASTS_ROOT=gs://boston-resilience-twin-data/forecasts`) is public (`allUsers` holds `run.invoker`). Both run the same image, in project 2, us-east1. The process refuses to start with `SERVING_ROUTES` unset or unknown.

- Chosen against: one private service reached through `gcloud run services proxy`, and one public service with an in-app token check (alternatives below).
- Why: IAM on the service is checked by Google before any of our code runs, so a bug in the app cannot expose `/predict`. The map page and the forecast rows are derived from public data (ADR-037 decision 2) and need no token.
- Breaks when: a route that must be private is added to the `web` route set. The route sets are in `Serving/src/serving/config.py` and tested; a new route needs its service chosen in the PR that adds it.

### 2. The model enters only as the build context `model`

`docker build --build-context model=<folder>` copies `model.joblib` and `metadata.json` into the runtime stage. The Dockerfile's `.dockerignore` excludes every `*.joblib` from the main context, so the model cannot arrive any other way. The deploy script downloads the registry version's files from the model bucket, checks every sha256 that `metadata.json` lists, and builds only when all match.

- Chosen against: the service downloading the model from the bucket at startup (a second identity and a runtime dependency on the model bucket), and the model in the git repository (the repository is public and holds no data or model, CLAUDE.md).
- Why: a revision is then one immutable artifact: image digest, model version and commit move together, and a rollback is a traffic move with nothing to fetch.
- Breaks when: the image grows past what a build and a rollback can move in minutes (the image is about 1 GB uncompressed today), or a model is too large to bake in.

### 3. The hash check before unpickling, and the threshold check

At startup the service hashes every file `metadata.json` lists that is in the folder and calls `joblib.load` only when all match. After loading, the threshold in `metadata.json` must equal the one embedded in the model, exactly. Any failure leaves the model unloaded and `/health` answers 503. Both Cloud Run services carry a startup probe on `/health` (5 s period, 12 failures allowed), so a revision whose model fails either check never becomes ready and never gets traffic.

- Chosen against: checking only at build. The build check stops a bad model reaching the image; the startup check stops a bad image (a wrong folder, a rebuilt file) reaching traffic. Each catches what the other cannot.
- Breaks when: the model takes longer than 60 s to load. Raise `failure_threshold` in `cloud_run.tf`.

### 4. One runtime account, with read scoped to `forecasts/`

`brt-serving` (project 2) runs both services. Its only grant is `roles/storage.objectViewer` on `boston-resilience-twin-data` (project 1), with an IAM condition on the object name prefix `forecasts/`. That needs uniform bucket-level access, which the bucket has. The service reads an object by name through the JSON API (`alt=media`) and never lists, so `storage.objects.get` is all it needs. `ci-writer` gets `roles/run.developer` on project 2 and `roles/iam.serviceAccountUser` on `brt-serving` only; `modelRegistrar` is unchanged.

- Chosen against: one account per service, and bucket-wide read (ADR-037 decision 8's table).
- Why: the prefix is free to scope and the service never needs more. Two accounts would add a grant and an actAs binding for a split the app does not need.
- Breaks when: the code that runs in `brt-model` reads forecasts. The identity allows it: the split is by route, not by account. A missing object inside the prefix is a 404, and one outside it is a 403; `forecasts.py` maps 404 to "no forecast".

### 5. Request-based billing and small scaling limits

Both services: minimum instances 0, 1 vCPU, 512Mi, `cpu_idle = true` (CPU billed only while a request is served), startup CPU boost, request timeout 30 s. Maximum instances: 2 for `brt-model`, 1 for `brt-web`.

- Chosen against: minimum instances 1 (ADR-037 names it as the fix for a cold start that times out the evening run).
- Why: scale to zero keeps the idle cost at storage cents (see Consequences). The evening run calls `/predict` once a day, and a cold start (container start plus model load, seconds) fits its timeout.
- Breaks when: the evening run's call times out on a cold start (set minimum instances 1 for `brt-model` and price it), or a flood of requests to the public `brt-web` (see "Where it breaks").

### 6. Terraform creates, the deploy script deploys

Terraform creates both services with the public placeholder image `us-docker.pkg.dev/cloudrun/container/hello` and owns their environment, scaling, probes, identity and IAM. `lifecycle.ignore_changes` covers exactly what `Serving/scripts/deploy.py` changes: `template[0].containers[0].image`, `template[0].labels`, `template[0].revision`, `client` and `client_version`. A later `terraform apply` therefore never rolls back a deployed revision. `traffic` stays Terraform's (latest revision, 100 percent). The script uses the Cloud Run Admin API v2 over google-auth's `AuthorizedSession`, as `vertex_client.py` calls Vertex, and runs on a laptop or a runner with only `google-auth` and `requests` (`Serving/requirements-deploy.txt`). It never calls gcloud. It reads its settings from `Model-Development/registry/registry.csv` and `Serving/deploy.csv`, and refuses a service whose live `SERVING_ROUTES` differs from `deploy.csv`.

- Chosen against: Terraform deploying the image, and gcloud (alternatives below).
- Breaks when: a field the script changes is not in `ignore_changes`, or Terraform changes a field the script depends on. The script's PATCH sends the service's own container back with only `image` changed, because a repeated field in an update mask is replaced whole: a PATCH of `containers` with only an image would wipe the environment, resources and probe.

### 7. Deploys are by digest, labelled with the version id and commit

The script builds `<repository>/brt-serving:v<version>-<commit12>`, pushes it, reads the pushed digest, and PATCHes both services to `<repository>/brt-serving@sha256:<digest>` with the template labels `model-version=<id>` and `git-commit=<commit12>`. A tag is never what a service runs. It refuses a tree with uncommitted changes under `Serving/`, `docs/contracts/schemas/`, `Frontend/` or the lock (a dry run only warns), so every image traces to a commit. `.github/workflows/deploy-serving.yml` runs it on `workflow_dispatch` from `main` as `ci-writer`.

- Chosen against: deploying by tag (a tag can move under a running service), and an automatic deploy on every merge (a deploy is a decision, until D12 builds the promotion path).
- Breaks when: two deploys run at once (the workflow has a concurrency group), or a revision fails: its operation fails, the script prints the revision's condition message and exits 2, and the traffic stays on the previous revision.

### 8. `model_version` is `0` for a local model that is not registered

`MODEL_VERSION` is a build argument and the image's environment variable; the contract's `model_version` is the registry version id. A model built locally with `Serving/scripts/local_model.py` has no registry version, so the compose profile builds it with `0`. The deploy script always builds with the registry version id and never with `0`.

- Breaks when: a consumer treats `0` as a real version. The contract pattern allows it; a monitor that groups by `model_version` must read `0` as "local".

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| One private service reached through `gcloud run services proxy` | None extra | Low | The public map has no server to read forecasts through; the proxy is a developer tool, not a path for the VM or a browser |
| One public service with an in-app token check | None extra | Low | A bug in the check, or a route added without it, exposes `/predict` with nothing in front |
| Terraform deploys the image | None extra | Low | Each deploy is a plan and an apply by a person with IAM rights; every `terraform apply` also risks rolling back a revision |
| gcloud instead of the Admin API | None extra | Low | A tool outside the lock on every runner and laptop, and its output to parse; the Admin API is what the other scripts already use |
| A Vertex AI endpoint | About 70 dollars a month | Medium | Rejected in ADR-037 decision 1 |
| Model downloaded by the service at startup | A second grant and a startup dependency | Medium | A revision can start with a different model than the one tested |
| **This ADR** | Cloud Run at scale to zero, cents of image storage | Medium | Each decision's "breaks when" |

## Consequences

- Terraform in `Deployment/terraform-project2/` adds eight resources (the Cloud Run API, two services, their invoker bindings, `brt-serving`, and two `ci-writer` grants) and `Deployment/terraform/` adds one (the conditional bucket grant). Prit applies project 2 first, because the grant in project 1 names an account that must exist.
- The first deploy is the proof of three things no plan shows: the placeholder image passes the `/health` startup probe (the Cloud Run hello image is believed to answer any path with 200, which a plan cannot show), the public binding is allowed by project 2's organization policy, and the PATCH keeps the environment, probe and resources. The runbook gets these checks in #183.
- The deploy workflow cannot be run before it merges: `ci-writer` is usable from `main` only (ADR-030). Its shape is held by tests in `Serving/tests`.
- Each deploy adds one image to `brt-images`. Cost: Cloud Run is free at idle and cents per evening; an image is cents a month. Teardown: a targeted `terraform destroy` for each new resource, then delete the `brt-serving` images (commands in the PR).
- #183 writes the README and the runbook for these commands.

## Where it breaks

- `traffic` is still Terraform's, so D12's traffic splits (a revision with no traffic, a smoke test, then the move that ADR-037 decision 1 describes) must add `traffic` to `ignore_changes` in the same PR, or the next apply moves the traffic back to the latest revision.
- The public `brt-web` under a sustained flood. Maximum instances 1 and the default concurrency cap what one instance serves, and beyond that Cloud Run answers 429. The cost is bounded by one instance running all month (about 66 dollars at list price, before the free tier) plus 40 cents per million requests it serves and egress. Budget alerts (ADR-030) are the only backstop; nothing stops the spend. The first move in a flood is to delete `brt-web` with a targeted `terraform destroy` (the map goes dark, nothing else does) and recreate it after.
- One runtime account: the model service could read forecasts if its code did so (decision 4).
- `brt-images` has no cleanup policy, and each deploy adds an image of about 1 GB uncompressed. A cleanup policy is a Terraform change to `artifact_registry.tf`, kept out of this PR because the training image shares the repository and a keep rule must name it.

## Revisit when

- D12 builds rollouts and traffic splits (decision 6, "Where it breaks").
- A cold start times out the evening run (decision 5), or the image outgrows a minutes-long rollback (decision 2).
- `brt-web` shows unexpected traffic or spend.
- The bucket starts holding non-public data under `forecasts/` (decisions 1 and 4; ADR-037 decision 8).
- A second route set or a second consumer of `/predict` appears (decision 1).
