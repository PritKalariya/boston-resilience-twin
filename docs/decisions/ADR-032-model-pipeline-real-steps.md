# ADR-032: The model pipeline on real steps: runner-side data pull, a pinned training image, reload check on every ref

- Status: accepted
- Note (2026-11-14): #123: in decision 8, "the push is skipped" means the registry push and every step after Gates (reload check, set champion). The training image is pushed right after the build on every main run, pass or fail (decision 6), so a failed gate leaves an image in brt-images with no model registered against it.
- Date: 2026-11-11
- Owner: PritKalariya
- Phase: model

## Context

ADR-028 froze the shape of the model workflow (`.github/workflows/model.yml`) on stub steps.
Since then #128 and #136 merged the real training code (`train.py`, `selection.py`, ADR-027),
and #131 applied project 2 (Workload Identity Federation, `ci-reader`, `ci-writer`, Artifact
Registry, ADR-030). PR 3 of #112 puts the real steps behind the workflow.

Two facts from the earlier image work shape this ADR. LightGBM loads the OpenMP runtime, so an
image without `libgomp1` fails at `import lightgbm`; `airflow.Dockerfile` installs it for the
Airflow image (ADR-022), and the training image needs the same, in one root apt step. And the
training code reads four files outside `Model-Development/`: the labels and features parquet
files, the feature catalog and `Data-Pipeline/dvc.lock`, whose sha256 is the data version.

The registry and champion steps are still stubs (#111, Kirtan) and the per-group bias counts
are an interim (#110, Alekhya); both are marked in their files with the interface the owner plugs
into.

## Decision

1. **The data pull runs on the runner**, with DVC and the `ci-reader` or `ci-writer` credentials.
   Only the files training needs are mounted, read-only, into the image: the labels and features,
   `feature_catalog.csv` and `dvc.lock`. The image holds no DVC repo and no credential.
2. **Requirements.** `Model-Development/requirements.txt` is the image's package list and
   `Model-Development/requirements-pull.txt` (`dvc`, `dvc-gs`) is the runner's. Every pin is copied
   from `Data-Pipeline/requirements.txt`, including the `xgboost-cpu` platform marker, and
   `test_steps_requirements.py` fails when a pin differs or a package the scripts import is missing.
3. **Writes.** `artifacts/runs`, `artifacts/mlruns` and `artifacts/reports` are bind-mounted over
   `/app/Model-Development/{runs,mlruns,reports}`. `MLFLOW_TRACKING_URI` stays empty, so MLflow uses
   its local store, which is uploaded with the artifacts.
4. **The winner** is the first candidate by `selection.py`'s ranking (`selection.rank`: validation
   F1 exact, then validation log loss, then name), without its margin check. `gates.py` alone
   decides promotion.
5. **The reload check** loads the winner's `model.joblib` in a fresh process, scores the run's scored
   validation rows and compares with the probabilities training wrote, at an absolute tolerance of
   1e-9, and the flags exactly. Any difference, or a file that cannot be loaded, exits 2. It runs on
   every ref, not only main: it has no side effects and catches a broken artifact before merge.
   `config/reload_fixture_row.json` is deleted.
6. **The image push** is main only, right after the build, tagged
   `us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images/brt-train:<git sha>`. The registry
   digest goes to `artifacts/image_digest.txt`; on a pull request it holds the local image ID.
7. **Identities.** `ci-reader@boston-resilience-twin-p2.iam.gserviceaccount.com` on pull requests and
   branches, `ci-writer@boston-resilience-twin-p2.iam.gserviceaccount.com` on main, which is the
   only ref that can use it, through the provider
   `projects/570429679915/locations/global/workloadIdentityPools/github/providers/github-oidc`.
   They are public identifiers and are written into `model.yml` (#131, ADR-030).
   `id-token: write` is granted to the `pipeline` job only.
8. **A gate failure on a pull request does not fail `model-ci`.** Merging code does not promote a
   model. With gates exit 1 on a pull request the run records it, warns in the job summary and
   sends Slack "gate failed"; on main the job fails and the push is skipped. Exit 2 fails the job
   on every ref.
9. **The change filter** adds `Data-Pipeline/config/feature_catalog.csv` (and
   `.github/scripts/in_image.sh`) to `Model-Development/`, `model.yml` and `Data-Pipeline/dvc.lock`,
   so a change to a data input retrains (continuous delivery on code and data changes).
10. **Slack** on a failure names the failed step and says "not pushed". No test metric reaches Slack.
11. **`STUB_OUTCOME` and its dispatch input are removed.** The 2026-11-06 dispatch runs of the stub
    workflow are the evidence for ADR-028's failure routing.
12. **Dockerfiles.** This ADR amends ADR-022 and ADR-028. `libgomp1` is installed in one root apt
    step (`--no-install-recommends`, apt lists removed). `brt-airflow` keeps `USER airflow` before
    pip. The training image runs pip as root and then switches to its non-root `train` user; the
    workflow runs the image with `--user` set to the runner's uid so `artifacts/` stays the runner's.
    ADR-028's step bodies, mounts, push, identities and gate handling change as above; its shape
    (one required `model-ci`, the step contract, Slack routing) does not.

Version stamps and windows (Sculley 2015, Xu 2024): `registry.json` carries the data version, git
commit, MLflow run id, image digest, both gate hashes, the train, validation and test windows
and the decision. Selection uses validation only and test is touched once (Xu 2024, Domingos
2012): no step reads a test metric. The seasonal baseline and every candidate run through every
stage first (Paleyes 2022).

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Pull the data in the image | none | medium | There is no DVC repo in the image, and the credentials would have to enter the container where every step runs. |
| Reuse `Data-Pipeline/requirements.txt` for the image | none | low | It carries Airflow's constraints URL, which belongs to the Airflow image, and geopandas and the geo stack, which training never imports; the image grows and the build needs network access to GitHub raw. |
| A chmod in the Dockerfile instead of mounts | none | low | A path made writable in the image still loses its data when the container exits, so a later step finds nothing. |
| Use `selection.py`'s exit code for the winner | none | low | Its exit 1 (no candidate reaches the margin) is read as "pipeline failed", and the gates, which own that decision, never run. |
| A committed fixture row for the reload check | none | low | It goes stale when the feature catalog changes, and the check then fails or passes on a row the model no longer scores. |
| A gate failure blocks merging | none | low | One weak data version would freeze all model code changes, and the fix would have to wait for data. |

## Consequences

- #110 replaces the per-group counts in `step_evaluate.py` and #111 replaces `step_rescore_champion.py`,
  `step_registry_push.py` and `step_set_champion.py`, each keeping the command line, the exit codes
  and the files named in its `INTERFACE FOR #n` block. Those blocks are in the PR body.
- A fork pull request gets no OIDC token, so the Google auth step and the run fail. A maintainer reruns
  it from a branch of this repository.
- Every run on main adds an image to Artifact Registry, about 1.3 GB uncompressed per commit. #111
  or a cleanup policy in Terraform bounds that growth.
- The uploaded artifacts hold the run folders and the MLflow store, which include each model's test
  metrics (training writes them once, ADR-027). They are artifacts of a public repository.
- `mlflow` is installed without Airflow's constraints, so its transitive packages are not pinned;
  `pip check` at the end of the build is the guard, and the image digest in `registry.json` names
  exactly what ran.
- The Terraform grant for the CI identities (#131) must be applied before the first pull request
  run can pull.

## Revisit when

- the run exceeds 30 minutes
- the image exceeds 2 GB
- #111 needs the registry push before the reload check
