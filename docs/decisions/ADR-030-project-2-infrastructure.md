# ADR-030: Project 2 infrastructure provisioned by Terraform, with two keyless CI identities

- Status: proposed
- Date: 2026-11-06
- Owner: Kirtan10
- Phase: model
- Amends ADR-009: Cloud Storage for model files is in project 2, not project 1

## Context

Issue #105 (M2) sets up project 2 (ADR-009) before anything registers a model or pushes an image: #111 (registry push and rollback) and #112 part 3 (training image push and GCP access from CI) both wait on it. ADR-006 makes the Vertex AI Model Registry in project 2 the only registry and left two choices to project 2's Terraform: where model files live and the registry's region. ADR-015 set the pattern for project 1, and its "Revisit when" asked project 2 to apply the same lessons.

Facts checked on 2026-11-06:

- Project 2 did not exist. The owner's account is in its own organization (`patel-k31-org`), and the education billing account (USD, the owner is `roles/billing.admin`) had no project linked.
- That organization's effective policies allow Workload Identity providers and members from other domains, and enforce `iam.disableServiceAccountKeyCreation`.
- Google does not allow `roles/owner` to be granted through `setIamPolicy` to an account outside the project's organization: it must be a console invitation the person accepts.

Prit (ADR-009's owner and #105's reviewer) answered the open questions on #105 on 2026-11-06: a project 2 bucket for model files with public access prevention; his northeastern.edu account for admin access; two CI identities, a reader for any branch and a writer for main only; and he grants both read access to the project 1 DVC remote in project 1's Terraform.

## Decision

`Deployment/terraform-project2/` holds project 2's Terraform, separate from project 1's `Deployment/terraform/`: its own state, its own variables, nothing shared.

**Bootstrap (by hand, once).** Terraform cannot create the bucket its own state lives in, and a project must exist before anything is created in it. These three steps are the only resources outside Terraform, as project 1's state bucket is (ADR-015):

```bash
gcloud projects create boston-resilience-twin-p2 --name="Boston Resilience Twin P2" --organization=797917992189
gcloud billing projects link boston-resilience-twin-p2 --billing-account=<education billing account>
gcloud storage buckets create gs://boston-resilience-twin-p2-tfstate --project=boston-resilience-twin-p2 --location=us-east1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://boston-resilience-twin-p2-tfstate --versioning
```

**What Terraform creates.**

| Resource | Choice |
|---|---|
| APIs | Vertex AI, Artifact Registry, Billing Budgets, IAM, IAM Credentials, STS, Storage, Resource Manager, Service Usage. Cloud Run and Gemini wait for Deployment |
| Region | `us-east1` for everything, the registry included (ADR-006 left it open; project 1's bucket and VM are there too) |
| Model files | Bucket `boston-resilience-twin-p2-models`: us-east1, versioned, uniform access, public access prevention enforced, `force_destroy` off |
| Images | One Docker repository, `brt-images`, in us-east1 |
| Registry | Nothing to create: model versions and aliases are data #111 writes through the API (ADR-006). No endpoint is created (endpoints bill by the hour) |
| Budget | $50 with email alerts at 25, 50 and 80 percent of actual spend and at 80 percent of forecast spend, measured on usage before credits |

**CI identities.** One Workload Identity pool (`github`) with one OIDC provider for GitHub Actions, whose condition accepts only tokens from `PritKalariya/boston-resilience-twin`, matched on its numeric `repository_id` and `repository_owner_id` (a name can be reclaimed after a rename, an id cannot). Two service accounts, no keys:

| Account | Who can use it | Roles in project 2 |
|---|---|---|
| `ci-reader` | Any run of the repository, pull requests included (bound on `attribute.repository_id`) | None. Prit grants `storage.objectViewer` on the project 1 data bucket, for `dvc pull` |
| `ci-writer` | Runs on `refs/heads/main` only (bound on `attribute.ref`; PR runs carry `refs/pull/<n>/merge`) | Custom role `modelRegistrar`; `artifactregistry.writer` on `brt-images` only; `storage.objectCreator` and `storage.objectViewer` on the model bucket. Prit grants the same project 1 read |

`modelRegistrar` holds `aiplatform.models.upload`, `.get`, `.list`, `.update` (aliases) and `aiplatform.operations.list`. `objectCreator` cannot delete, so it cannot overwrite an object: each registered version keeps its files, which a rollback needs (ADR-006). Fork pull requests get no OIDC token, so they cannot use either account.

**Teammate access.** `roles/editor` and `roles/resourcemanager.projectIamAdmin` for the members in `project_admins` (Prit), instead of `roles/owner`, which Terraform cannot grant to an account outside the organization. Together they cover reviewing plans and debugging CI, IAM included; they leave out billing and project deletion.

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Terraform creates the project too | None | Medium | State still needs an existing bucket, so it would sit in project 1's state bucket and break "its own state bucket" |
| Reuse the default "My First Project" | None | Low | A random project ID in every config and URL, in a project never meant for this |
| One CI identity for every branch | None | Low | Any PR could push images and register models |
| `roles/aiplatform.user` for the writer | None | Low | Also deletes models, trains, deploys endpoints and calls Gemini: a leaked token costs money |
| Model files in project 1's data bucket (ADR-006 option) | None | Medium | Cross-project grants for the registry and Cloud Run, and the bucket's 30-day noncurrent rule deletes the old copy of any overwritten file (#48) |
| `roles/owner` for Prit through a console invite | None | Low | Click-ops outside Terraform, against CLAUDE.md |
| Budget on net spend (credits included) | None | Low | Education credits make net spend about 0, so no alert ever fires |

## Consequences

- #111 uploads with `ci-writer` through `google-github-actions/auth` with the `workload_identity_provider` output, writes each version's files under its own path in the model bucket, and skips the push without project 2 access.
- #112 part 3 authenticates PR runs as `ci-reader` and main runs as `ci-writer`, and pushes the training image to the `image_repository` output.
- Prit adds both service account emails (outputs `ci_reader_email`, `ci_writer_email`) as `storage.objectViewer` on `boston-resilience-twin-data` in project 1's Terraform.
- The Vertex AI service agent reads model files and images in its own project with its default role; if an upload fails on access, the fix is a grant to that agent here, not a broader CI role.
- Budget alerts email the billing account's admins: the project 2 owner.
- Costs: APIs, service accounts, the pool, the budget and the registry are free while nothing is deployed. Storage and images are cents a month. Teardown: `terraform destroy -var-file=terraform.tfvars` in `Deployment/terraform-project2/` after emptying the model bucket, then delete the state bucket and the project by hand.

## Revisit when

- Deployment adds Cloud Run, Gemini or a runtime service account: each gets its own grants here.
- The repository moves to another owner or is recreated (the provider condition and the reader binding use its ids; a rename alone changes nothing).
- A Vertex AI endpoint is proposed (hourly cost).
- Project 2 joins an organization that Prit's account belongs to, which would allow a direct `roles/owner` grant.
