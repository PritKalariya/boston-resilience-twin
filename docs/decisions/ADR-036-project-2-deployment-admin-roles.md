# ADR-036: Project 2 admins get Cloud Run and service account IAM for Deployment

- Status: proposed
- Date: 2026-11-20
- Owner: Kirtan10
- Phase: deploy
- Amends ADR-030 "Teammate access": `project_admins` also hold `roles/run.admin` and `roles/iam.serviceAccountAdmin`. Only ADR-030's status line changes, to point here.

## Context

ADR-030 gave the members of `project_admins` (Prit) `roles/editor` and `roles/resourcemanager.projectIamAdmin` in project 2, in place of `roles/owner`, which cannot be granted through the API to an account outside the project's organization. That pair covered reviewing plans and debugging CI.

#161 (D7) has Prit plan and apply project 2 Terraform for the Cloud Run serving service, and D19's agent service follows the same pattern. Two of its grants are IAM on a single resource, which Editor cannot set and Project IAM Admin does not cover (it sets the project's policy only):

- `run.services.setIamPolicy`: `run.invoker` for `airflow-vm-sa` on the serving service (`/predict` is not public), later on the agent service;
- `iam.serviceAccounts.setIamPolicy`: `iam.serviceAccountUser` (actAs) for `ci-writer` on the runtime service account only, so it can deploy revisions that run as it.

Deployment changes no custom role (`ci-writer` deploys with `roles/run.developer`, so `modelRegistrar` keeps its no-deploy meaning) and grants nothing on secrets, buckets or `brt-images` in project 2. Editor already reads the budget for `terraform plan` (checked by Prit on 2026-11-20: `Plan: 4 to add` with no error on `google_billing_budget`), so no billing account role is needed.

## Decision

Add `roles/run.admin` and `roles/iam.serviceAccountAdmin` to `admin_roles` in `Deployment/terraform-project2/iam.tf`, granted to every member of `project_admins`, through Terraform like the rest of ADR-030. Checked with `gcloud iam roles describe`: `run.admin` holds `run.services.setIamPolicy`, and `serviceAccountAdmin` holds `iam.serviceAccounts.setIamPolicy` but neither `iam.serviceAccounts.actAs` nor `iam.serviceAccountKeys.create`.

Still not `roles/owner`. Billing, project deletion and the bootstrap (ADR-030) stay with the project 2 owner.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| `roles/owner` through a console invite | None | Low | Click-ops outside Terraform, against CLAUDE.md (ADR-030) |
| `roles/iam.securityAdmin` | None | Low | About 300 `setIamPolicy` permissions across services, for the two Deployment needs |
| `roles/iam.roleAdmin` | None | Low | No custom role changes in Deployment; Editor already reads custom roles for plan |
| `roles/billing.viewer` on the billing account | None | Low | Not needed for plan, and shows payment details and spend for the whole billing account |
| The project 2 owner applies every Deployment change | None | Medium | Every serving and agent change waits on one person before the Dec 6 deadline |

## Consequences

- `roles/resourcemanager.projectIamAdmin` plus `roles/iam.serviceAccountAdmin` lets the holder grant itself actAs on any project 2 service account, `ci-writer` included, and so act as it. That is effectively owner without billing. It is contained by everything being in Terraform: a grant made outside it shows up as drift in the next `terraform plan`, and every change to `iam.tf` is a reviewed PR.
- `run.admin` also deletes Cloud Run services and changes their traffic; a mistake there is undone by the deploy script (#161), which redeploys the production version.
- The project 2 owner still applies changes to `project_admins` itself, since the new roles exist only after the first apply.
- Cost: none; IAM grants are free. Teardown: `git revert <merge sha>`, then `terraform apply -var-file=terraform.tfvars` in `Deployment/terraform-project2`.

## Revisit when

- Deployment needs IAM on another resource type in project 2 (secrets, buckets, repositories): add the narrowest role for it here.
- Deployment ends: drop the two roles back to ADR-030's pair.
- Project 2 joins an organization that Prit's account belongs to (ADR-030), which would allow a direct `roles/owner` grant.
