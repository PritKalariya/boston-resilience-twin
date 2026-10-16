# Runbook

Operational steps for the Boston Resilience Twin. Update this file in the PR
that changes an operational step.

## Project 1 infrastructure (Terraform)

Config lives in `Deployment/terraform/`. It covers project 1 only (ADR-009):
the Airflow VM, the data bucket, the VM service account, two empty secret
slots, the project 1 APIs, and IAM. Nothing for Cloud Run, Artifact Registry,
Vertex, or Gemini.

State is in the GCS bucket `boston-resilience-twin-tfstate` under the prefix
`project1`. That bucket is not managed by Terraform.

### Cost before you apply

| Resource | Running | Stopped or idle |
|---|---|---|
| `airflow-vm` (e2-medium, us-east1) | about $24.46 a month | $0 of compute |
| 30 GB pd-balanced boot disk | about $3 a month | about $3 a month, billed even when the VM is stopped |
| Ephemeral external IPv4 | about $3.65 a month ($0.005/hour) | $0, released when the VM stops |
| `boston-resilience-twin-data` bucket | storage charged per GB stored | same |
| Service account, secret slots, IAM, API enablement | $0 | $0 |
| **Total** | **about $31.11 a month** | **about $3 a month** |

Budget is $50 of credits per member and is not pooled. The VM stays stopped
when idle in October and runs 24/7 from Nov 1.

### Apply

One time, if not already done:

```bash
gcloud auth application-default login
gcloud config set project boston-resilience-twin
```

Then:

```bash
cd Deployment/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in the real addresses
terraform init
terraform plan -var-file=terraform.tfvars -out=tfplan
terraform apply tfplan
```

`terraform.tfvars` and `tfplan` are gitignored. Never commit either.

Run `terraform plan` once more after the apply. A clean plan ("No changes")
is the check that the config matches what is actually in the project.

### Fill the secret slots

Terraform creates empty secrets. Values are added out of band and never go
into a `.tf` file or a tfvars file:

```bash
printf '%s' "$NOAA_CDO_TOKEN" | gcloud secrets versions add noaa-cdo-token --data-file=-
printf '%s' "$CENSUS_API_KEY" | gcloud secrets versions add census-api-key --data-file=-
```

### Stop the VM when idle

```bash
gcloud compute instances stop airflow-vm --zone=us-east1-b
```

Start it again with:

```bash
gcloud compute instances start airflow-vm --zone=us-east1-b
```

The external IP is ephemeral, so the VM gets a different address after every
start. Look it up with:

```bash
gcloud compute instances describe airflow-vm --zone=us-east1-b \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

SSH goes through OS Login:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b
```

Two IAM facts make that work, both of them in Terraform: every team member has
`roles/compute.osLogin`, and every team member has
`roles/iam.serviceAccountUser` on `airflow-vm-sa` alone. The second one is
needed because the VM has a service account attached, and logging in to such a
VM counts as acting as that service account. The grant is scoped to the one
service account, not the project.

A grant to a member who has never signed in to GCP fails with
"User ... does not exist". Each member signs in once, then `terraform apply` is
re-run to pick up the grants that failed.

A stopped VM still plans clean: the config ignores the attributes that move on
their own (the ephemeral IP, the resolved boot image, OS Login SSH keys).

### Tear down

```bash
cd Deployment/terraform
terraform destroy -var-file=terraform.tfvars
```

The data bucket has `force_destroy = false`, so a destroy fails while objects
are still in it. Empty it first, deliberately, only when the data is no longer
needed:

```bash
gcloud storage rm -r --all-versions gs://boston-resilience-twin-data/**
```

**The state bucket is not destroyed by Terraform.** `boston-resilience-twin-tfstate`
is outside this config on purpose, so a destroy cannot delete the state it is
reading. Remove it by hand, last, after the destroy has finished:

```bash
gcloud storage rm -r --all-versions gs://boston-resilience-twin-tfstate
```
