# Project 1 infrastructure (Terraform)

Provisions the Project 1 half of the Boston Resilience Twin (ADR-009): the Airflow
VM, the data bucket (raw data and DVC remote), a VM service account, nine empty
Secret Manager slots (three API keys, five Airflow secrets, the Slack webhook), and team IAM. Scoped to project boston-resilience-twin, region
us-east1. State lives in gs://boston-resilience-twin-tfstate, created by hand and
never destroyed by Terraform.

The data bucket is versioned, so every overwritten raw pull is kept as a
noncurrent version. A lifecycle rule deletes those noncurrent versions 30 days
after they become noncurrent. The live version of an object is never touched.

## Cost

| State | Monthly |
|---|---|
| VM running | about $31 (e2-medium $24.46 + 30 GB pd-balanced disk $3 + ephemeral IP $3.65) |
| VM stopped | about $3 (disk only; the ephemeral IP is released on stop) |

The VM runs 24/7 only for the live run and for tests, and is stopped otherwise
(ADR-029). Its startup script (`startup.sh`) installs Docker, clones the repo and
renders the repo's `.env` from Secret Manager on every boot (ADR-034). Budget is $50
credits per member, not pooled. The secret slots cost nothing until a version exists,
and a version costs a few cents a month.

## Testing the .env render

The render in `startup.sh` runs alone on a laptop: no root, no Docker, no cloud call.
`BRT_RENDER_ONLY=1` skips the provisioning; the target path, the owner, the project and
the fetch command are overridable. The fetch command is called with a secret name and
prints the value (exit 0), exits 4 for "no version" or exits 1 for any other failure.
A stub that reads one file per secret from `$STUB_DIR`:

    #!/usr/bin/env bash
    [ -e "$STUB_DIR/$1.fail" ] && exit 1
    [ -e "$STUB_DIR/$1" ] || exit 4
    cat "$STUB_DIR/$1"

    mkdir -p /tmp/brt/repo /tmp/brt/secrets    # then one file per secret in /tmp/brt/secrets
    STUB_DIR=/tmp/brt/secrets BRT_RENDER_ONLY=1 BRT_ENV_FILE=/tmp/brt/repo/.env \
      BRT_ENV_OWNER="$(id -u):$(id -g)" BRT_SECRET_FETCH_CMD=/tmp/brt/stub_fetch.sh \
      bash Deployment/terraform/startup.sh

Use only made-up values in the stub's files. `shellcheck Deployment/terraform/startup.sh`
is clean.

## Teardown

Stop the VM (keeps everything; ends about $28 of the ~$31 monthly charge, while
the disk keeps billing about $3):

    gcloud compute instances stop airflow-vm --zone=us-east1-b

Empty the data bucket first, including old versions (force_destroy is off by
design, so a non-empty bucket makes destroy fail loudly):

    gcloud storage rm -r --all-versions gs://boston-resilience-twin-data/**

Then destroy all Project 1 resources:

    cd Deployment/terraform && terraform destroy -var-file=terraform.tfvars

The state bucket is not destroyed by Terraform. Remove it by hand only when the
project is over:

    gcloud storage rm -r --all-versions gs://boston-resilience-twin-tfstate

Full operational procedure: see ../../docs/runbook.md
