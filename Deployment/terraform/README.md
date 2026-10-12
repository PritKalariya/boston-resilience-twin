# Project 1 infrastructure (Terraform)

Provisions the Project 1 half of the Boston Resilience Twin (ADR-009): the Airflow
VM, the data bucket (raw data and DVC remote), a VM service account, two Secret
Manager slots, and team IAM. Scoped to project boston-resilience-twin, region
us-east1. State lives in gs://boston-resilience-twin-tfstate, created by hand and
never destroyed by Terraform.

## Cost

| State | Monthly |
|---|---|
| VM running | about $31 (e2-medium $24.46 + 30 GB pd-balanced disk $3 + ephemeral IP $3.65) |
| VM stopped | about $3 (disk only; the ephemeral IP is released on stop) |

The VM is stopped when idle in October and runs 24/7 from Nov 1. Budget is $50
credits per member, not pooled.

## Teardown

Stop the VM (keeps everything, ends the ~$24 compute charge):

    gcloud compute instances stop airflow-vm --zone=us-east1-b

Destroy all Project 1 resources (empty the data bucket first; force_destroy is off
by design, so a non-empty bucket makes destroy fail loudly):

    cd Deployment/terraform && terraform destroy -var-file=terraform.tfvars

The state bucket is not destroyed by Terraform. Remove it by hand only when the
project is over:

    gcloud storage rm --recursive gs://boston-resilience-twin-tfstate

Full operational procedure: see ../../docs/runbook.md
