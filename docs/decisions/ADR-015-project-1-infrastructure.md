# ADR-015: Project 1 infrastructure provisioned by Terraform

- Status: accepted, partially superseded by ADR-018 (who may operate the VM)
- Date: 2026-10-11
- Owner: kalariya.p
- Phase: setup

## Context

Issue #3 requires the first GCP project's base infrastructure: an Airflow VM, a data bucket doubling as the DVC remote, a service account for the VM, Secret Manager slots, and IAM for all six team members. ADR-009 splits the system across two projects; this is the project-1 half (VM, Cloud Storage, monitoring, Secret Manager). Everything here is provisioned by Terraform, per the no-click-ops rule in CLAUDE.md. Several choices below carry cost or security weight and are easier to get right before any state exists, which is why they are recorded now rather than discovered later.

## Decision

- **State backend:** a GCS bucket (boston-resilience-twin-tfstate), created by hand before the first run because Terraform cannot create the bucket that holds its own state. Versioned, private, public access prevention enforced. Terraform never destroys it; it is removed by hand at project end.
- **Region/zone:** us-east1 / us-east1-b. Matches the region the course labs use; low latency to Boston is not a factor for a batch pipeline.
- **VM:** e2-medium, 30 GB pd-balanced boot disk, Ubuntu 24.04 LTS, OS Login for SSH. Stopped when idle in October, 24/7 from Nov 1.
- **External IP:** ephemeral, not reserved. An ephemeral IP is released when the VM stops and bills nothing; a reserved IP bills even while stopped. This keeps the stopped-state cost to the disk alone.
- **No service account keys:** the VM uses its attached service account via the metadata server. No key file is ever created or downloaded, removing the most common credential-leak path for a public repo.
- **Secret Manager:** empty slots only (noaa-cdo-token, census-api-key). Terraform creates the slots; values are added by hand with gcloud so no secret value ever lands in state or the repo. Further secrets (email, Slack) are deferred to the components that read them.
- **Teammate VM access:** roles/compute.instanceAdmin.v1 (start, stop, SSH, reconfigure), plus project viewer, OS Login, data-bucket objectAdmin, and secret accessor. No project-wide delete and no IAM-admin. OS Login here is roles/compute.osLogin, which gives SSH without sudo, so operating the VM (Docker, Airflow) is owner-only by team agreement until the team decides who gets roles/compute.osAdminLogin (#49). IAM does not enforce this: instanceAdmin.v1 plus serviceAccountUser on the VM service account lets a teammate set instance metadata, such as a startup script, and gain root on the VM.
- **Terraform state access:** teammates can read state but not write it. As project viewers they hold roles/storage.legacyObjectReader on the state bucket by default, which lets a reviewer run `terraform plan -lock=false`; only the owner can write state or apply. The state holds no secret values by design.
- **Provider pinned to google ~> 8.0** (8.6.0 at apply), with .terraform.lock.hcl committed so all six members build identically.
- **IAM verification split to issue #29.** The non-owner IAM grants cannot be applied until each teammate has signed in to GCP once (see Consequences). That verification is tracked separately so #3 can close on the infra that is actually done.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Teammate role: custom least-privilege (start/stop/SSH only, no delete) | same | higher (custom role to define and maintain) | cannot delete the VM; stronger least-privilege |
| Teammate role: compute.instanceAdmin.v1 (chosen) | same | low (one predefined role) | a teammate can delete the VM, but terraform apply recreates it in ~10 min; state and data untouched |
| Reserved static IP | +$3.65/mo while stopped, double if left unattached | same | predictable address; bills when idle |
| Ephemeral IP (chosen) | $0 while stopped | same | address changes across stop/start; fine for SSH-via-OS-Login |
| SA key file for VM auth | same | same | a downloadable credential that can leak into a public repo |
| Attached SA via metadata (chosen) | same | same | no credential to leak; access is revoked by changing IAM |

## Consequences

- The stopped VM costs about $3/month (disk only); running costs about $31/month (VM $24.46 + disk $3 + ephemeral IP $3.65). Budget is $50 credits per member, not pooled.
- Because teammate access uses additive google_project_iam_member resources, a partial apply failure is recoverable: grants that succeed are kept, and a later apply retries only the missing ones. An authoritative policy resource would have failed all-or-nothing.
- **Team IAM depends on each member's first GCP login.** GCP rejects a grant to a @northeastern.edu identity that has never signed in to Cloud ("User ... does not exist"). The owner's grants applied; the other five failed on first apply for this reason. This will recur when project 2 is set up. The fix is operational, not code: each member signs in once, then apply is re-run.
- Choosing instanceAdmin.v1 means a teammate could delete the VM. Accepted because the blast radius is low (recreatable from code, data and state are in separate protected buckets) and the deadline favored a predefined role over a custom one.
- Team members also hold roles/iam.serviceAccountUser on airflow-vm-sa (added in #29). OS Login SSH to a VM with an attached service account requires it. It is scoped to that one service account, not the project, so members cannot act as any other service account, and it grants no GCP access beyond the bucket and secret access they already hold directly. Combined with instanceAdmin.v1 it does allow root on the VM (see Teammate VM access above).

## Revisit when
- The VM moves to 24/7 on Nov 1 and an accidental delete would cost more: reconsider the custom least-privilege role.
- Project 2 is provisioned: apply the same first-login lesson up front, and decide whether any project-2 service needs cross-project access to project-1 secrets.
- A reserved IP becomes necessary (e.g. a stable address for an exposed service), at which point the idle-cost tradeoff changes.
