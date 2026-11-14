# ADR-028: Model CI/CD workflow shape: stub steps, one required aggregator, Slack routing

- Status: accepted; amended by ADR-032 (2026-11-11): the real steps, the data pull, the image push, the identities, the reload check on every ref and the gate failure on a pull request
- Date: 2026-11-05
- Owner: PritKalariya
- Phase: model

## Context

Issue #112 builds the model CI/CD pipeline. ADR-026 froze the promotion gates
(`Model-Development/scripts/gates.py`). This ADR freezes the workflow around them
(`.github/workflows/model.yml`) so PR 3 swaps the contents of the steps, built on stubs
here, without changing the workflow's shape or the required check. No GCP resource is
used yet: the data pull, champion re-score and registry push are stubs.

## Decision

- **PRs run up to the gates; main adds the rest.** Order: pull data, train, evaluate,
  (main: re-score the champion), gates, then on main the reload check, registry push
  and champion step. A PR never pushes to the registry or touches the champion.
- **Step adapter contract.** One file per step in `Model-Development/scripts/steps/`,
  named `step_<name>.py` (the prefix keeps them from shadowing #128's `train.py` and
  `evaluate.py`, which Python would otherwise resolve first from the steps folder), run
  as `python step_<name>.py --artifacts DIR`. A step reads and writes only under the
  artifacts directory. Exit 0 is ok, 2 is a pipeline failure, and only the gates step
  (`gates.py`) exits 1, a gate failed (it also exits 2 when it cannot evaluate). PR 3
  replaces a step's body with a call into the owner's code (#108 or M4, #110, #111) and
  keeps the command line. `step_common.py` puts `Model-Development/scripts` on the path
  after the steps folder, so a step imports #128's modules by their own names.
  `STUB_OUTCOME` (`pass`, `fail_gate`, `fail_pipeline`) steers
  the stubs only.
- **Everything runs in the image except Slack.** `Model-Development/Dockerfile` (pinned
  `python:3.12` patch tag, non-root, builds only if the gates load) runs every step with
  `artifacts/` mounted. The artifacts directory also holds `image_digest.txt`, which the
  workflow writes right after the build for #111's registry record (`step_registry_push.py`
  copies it into `registry.json`): the local image ID until PR 3 pushes the image to
  Artifact Registry, after which it holds the registry digest. The notify step runs on the runner, the only place the webhook
  secret is exposed.
- **`model-ci` is the required check.** The workflow triggers on every PR. `changes`
  decides whether `Model-Development/`, the workflow file or `Data-Pipeline/dvc.lock`
  (a new data version should retrain) changed, with one path pattern for PRs and
  pushes: a PR is diffed against its base, a push to main against the previous head
  (`github.event.before`), and a diff that cannot be computed (a force push, a new
  branch) runs the pipeline anyway. A manual dispatch always runs. `pipeline` runs only
  when `changes` says so, and `model-ci` always runs: it fails if `changes` did not succeed, or if
  `pipeline` did not succeed when model files changed, and passes on a skipped
  `pipeline` when none did. Only `model-ci` is marked required.
- **Slack routing** (webhook in `SLACK_ALERT_WEBHOOK`, the Airflow pipeline's name):
  gates exit 1 is "gate failed", any other failure is "pipeline failed" (both on PRs and
  main), a success on main is "training complete" with the decision, a success on a PR
  or a cancelled run sends nothing. The message carries the repo, ref, run URL, data
  version, decision, F1 margin and gap when available. An empty secret (also fork PRs)
  or a Slack outage is a `::warning::`, never a failed run.

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| A path-filtered required workflow | none | low | A PR that skips the workflow never gets the check, which hangs on "expected" and blocks merging. |
| A skipped downstream job as the required check | none | low | A failed `changes` job skips the downstream job, and a skipped job counts as passed, so the failure merges. |
| Runner Python instead of the image | none | low | The workflow's shape changes in PR 3 when pinned requirements arrive. |
| Pushing to the registry from PRs | registry storage | low | Unmerged candidates pollute the registry and compete for champion. |

## Consequences

- PR 3 changes step bodies, `Dockerfile` requirements and `gates.csv`-adjacent inputs,
  not `model.yml`.
- The required check is added by a human in branch protection after the first merge.
- A run in the image adds a Docker build (seconds on the slim base, minutes once
  requirements are pinned).
- `SLACK_ALERT_WEBHOOK` must exist as a GitHub Actions secret for messages to send.
- The coverage bar is 70 percent for `Model-Development/scripts` and for
  `Data-Pipeline/scripts`, each on its own.

## Revisit when

- the full run exceeds 30 minutes
- GitHub changes how skipped jobs count for required checks
- M4, M7 or M8 needs a step outside the contract
