# ADR-019: Named secret writers with per-secret versionAdder

- Status: accepted
- Date: 2026-10-16
- Owner: kalariya.p
- Phase: setup

## Context

ADR-015 gives the team read-only access to the two Secret Manager slots (roles/secretmanager.secretAccessor), and says values are added by hand with gcloud, never through Terraform. Only the owner could add values. Kirtan's W1-4 work needs to store the NOAA and Census keys he registered. Issue #50.

## Decision

- A new variable, secret_writers, lists who may add secret values. Its real value lives only in the gitignored terraform.tfvars, so no email is committed.
- Each writer gets roles/secretmanager.secretVersionAdder on each project 1 secret (google_secret_manager_secret_iam_member, one grant per writer and secret). Secret-level, never project-level.
- Current writer: Kirtan (W1-4 owner).
- Values are added with `gcloud secrets versions add <secret> --data-file=-`, typed or pasted at the prompt, so they stay out of shell history, chat, the repo, and Terraform state.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Owner adds the values | $0 | low | the token must travel from Kirtan to the owner over some channel, which breaks "values never go through chat" |
| Owner registers own keys | $0 | owner time | no transfer, but moves Kirtan's work onto the owner |
| Project-level secretVersionAdder | $0 | low | writer can add versions to every secret, including future ones it should not touch |
| roles/secretmanager.admin | $0 | low | writer can also delete secrets, destroy versions, and change IAM |
| Per-secret versionAdder for named writers (chosen) | $0 | low | writer can only add versions to the listed slots; cannot read beyond accessor, delete, or change IAM |

## Consequences

- secretVersionAdder only adds versions. Reading uses the existing secretAccessor; disabling or destroying versions stays with the owner.
- Adding or removing a writer is a one-line change to terraform.tfvars plus an apply. Reviewers see the change only as a plan count, since the list is not committed.
- The grant loops over every secret in local.secret_ids, so a writer automatically gets versionAdder on any new slot added later. That is intended while all slots share one writer list.
- The live test of the grant is Kirtan's first `versions add` in W1-4; this ADR's issue verified the binding with get-iam-policy.

## Revisit when
- A secret needs a different set of writers from the others: split secret_writers per secret.
- A secret holds something more sensitive than a free API key (for example the Gmail app password or Slack webhook): decide its writers explicitly before adding the slot.
