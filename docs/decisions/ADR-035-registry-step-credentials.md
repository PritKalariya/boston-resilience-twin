# ADR-035: Only the three main-only registry steps get Google credentials in the training image

- Status: proposed
- Date: 2026-11-14
- Owner: Kirtan10
- Phase: model
- Amends ADR-032 decision 1 ("The image holds no DVC repo and no credential") by reference: the image still holds none, and three steps get a credentials file mounted at run time. ADR-032 is not edited.

## Context

ADR-032 decision 1 keeps every credential out of the training image: the data pull runs on the
runner, and the image only gets the files training reads, read-only. #111 replaces the three
main-only stubs `step_rescore_champion.py`, `step_registry_push.py` and `step_set_champion.py`
with calls to `registry.py` (ADR-006), and all three need project 2:

- the re-score downloads the champion's `model.joblib` from the model bucket and scores it, so it
  needs the image's scikit-learn, LightGBM and XGBoost and cannot run on the runner;
- the push uploads files to the model bucket and registers a Vertex AI Model Registry version;
- the champion step moves the `production` and `previous` aliases.

`ci-writer`, which only `refs/heads/main` can use (ADR-030), already holds what they need: the
`modelRegistrar` role and `storage.objectCreator` and `storage.objectViewer` on the model bucket.
Vertex has no `aiplatform.operations.get` permission; `aiplatform.operations.list`, which the role
has, covers polling the upload operation (checked with `gcloud iam list-testable-permissions`).

The Google auth step (`google-github-actions/auth`) writes ci-writer's credentials file to the
workspace root as `gha-creds-*.json` and exports `GOOGLE_APPLICATION_CREDENTIALS`. The path cannot
be changed: the action writes to `$GITHUB_WORKSPACE` on purpose, so Docker-based actions can read
it. The file is a Workload Identity config (`external_account`) with the job's OIDC request token
and URL, not a Google token: google-auth exchanges it for short-lived tokens when it needs them,
and it stops working when the job ends.

## Decision

1. **The image still holds no credential**, and training, evaluate, the gates and the reload check
   run with none, on every ref.
2. **An opt-in mount in `in_image.sh`.** With `MOUNT_GCP_CREDENTIALS=1`, the file
   `GOOGLE_APPLICATION_CREDENTIALS` names is bind-mounted read-only at
   `/run/gcp/credentials.json` and the variable points there. Set but with no file, the script
   exits 2. Unset, nothing changes.
3. **Only three steps set it**, in their own `env:` in `model.yml`: "Re-score the champion",
   "Registry push" and "Set champion". All three run on main only, so the file is always
   ci-writer's. A test fails if any other step sets it.
4. **The workspace is never mounted.** `in_image.sh` mounts named files and folders only (ADR-032),
   none of which holds the credentials file, and a test fails if it mounts `$PWD` itself.
   `Model-Development/Dockerfile.dockerignore` names `gha-creds-*.json`, beside its allowlist that
   already keeps the workspace root out of the build, and `.gitignore` names it too.
5. On a laptop the same flag mounts the gcloud application-default credentials file (runbook
   "Model registry (#111, ADR-006)").

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Pass the auth step's `access_token` output as an environment variable | none | low | The token expires after an hour and is not refreshed, so a long run fails partway; `vertex_client.py` needs a second way to build credentials, and the token sits in the `docker run` environment. |
| Run the three steps on the runner instead of in the image | none | medium | The re-score must load the champion with the image's model libraries; on the runner they are other versions, or a second install to keep in step with the image. |
| Mount the credentials into every step | none | low | Training and evaluate, which need no project 2 access, could reach the registry and the model bucket, against ADR-032. |
| Copy the file to `$RUNNER_TEMP` first | none | low | The action's own file stays in the workspace anyway, so it adds a step and protects nothing. |

## Consequences

- The three registry steps reach project 2 as ci-writer on main. Pull requests, branches and forks
  never run them and never mount a credential.
- A new step that needs project 2 needs this ADR changed, not only a line in `model.yml`.
- The OIDC request token in the file works only while the job runs, so the mounted file is useless
  after the job; the auth action's post step deletes it.

## Revisit when

- a step other than these three needs project 2
- the auth action can write its credentials file outside the workspace
- the workflow moves to self-hosted runners, where the workspace is not discarded after the job
