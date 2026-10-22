# ADR-018: Sudo on the Airflow VM for named admins

- Status: accepted
- Date: 2026-10-16
- Owner: kalariya.p
- Phase: setup

## Context

ADR-015 gives every member roles/compute.osLogin (SSH without sudo) and says operating the VM is owner-only by team agreement until the team decides who gets roles/compute.osAdminLogin. That decision is issue #49 (W2-19). Running Docker and Airflow on the VM (W2-8) needs sudo, W3-8 has Srikar running the pipeline on the VM, and the owner is unavailable on weekends, so owner-only would stall VM work two days a week. The team was unavailable when this was decided, so it is an owner call, open to revision.

## Decision

- A new variable, vm_admins, lists who gets sudo. Its real value lives only in the gitignored terraform.tfvars, so no email is committed.
- Each admin gets roles/compute.osAdminLogin on the airflow-vm instance (google_compute_instance_iam_member), never at project level.
- Current admins: the owner and Srikar. Everyone else keeps roles/compute.osLogin, SSH without sudo.
- This supersedes ADR-015's line that VM operation is owner-only.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Owner only | $0 | none | blocks W3-8, and VM operations stall whenever the owner is away |
| Everyone | $0 | low | six operators on one shared VM; more accidental breakage for no current need |
| Project-level osAdminLogin | $0 | low | sudo on every future VM in project 1, not just this one |
| Owner plus Srikar, instance-scoped (chosen) | $0 | low | two operators cover each other; scope ends at airflow-vm |

## Consequences

- ADR-015 already records that roles/compute.instanceAdmin.v1 plus roles/iam.serviceAccountUser lets any member reach root through instance metadata, such as a startup script. So this grant does not create a new capability. It names who is expected to operate the VM, and gives them a direct, attributable path to do it.
- Adding or removing an admin is one line in terraform.tfvars plus an apply. Reviewers see the change only as a plan count, since the list is not committed.
- A future VM in project 1 does not inherit sudo from this grant.

## Revisit when
- The VM moves to 24/7 on Nov 1 and a mistake on it costs more.
- A third person needs to operate Airflow on the VM.
- Project 2 gets compute that needs operators.
