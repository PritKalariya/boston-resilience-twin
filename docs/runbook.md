# Runbook

Operational steps for the Boston Resilience Twin. Update this file in the PR
that changes an operational step.

## Local Airflow stack (Docker Compose)

`docker-compose.yaml` at the repo root runs Airflow 3.3.2 with LocalExecutor
and a Postgres 16 metadata database: `postgres`, a one-shot `airflow-init`,
`airflow-apiserver` (UI and API on 127.0.0.1:8080 only), `airflow-scheduler`
(also runs the tasks), and `airflow-dag-processor`. The image is pinned to
`apache/airflow:3.3.2-python3.12` (ADR-013). There is no triggerer: add the
official triggerer service back when the first deferrable operator lands,
since a deferred task waits forever without one.

### Start and check

```bash
docker compose up -d --wait
docker compose ps -a
curl -fsS http://127.0.0.1:8080/api/v2/monitor/health
```

Healthy means: four services `(healthy)`, `airflow-init` `Exited (0)`, and
the health JSON shows `metadatabase`, `scheduler` and `dag_processor` as
`healthy`. `triggerer` is `null` because none is deployed.

Smoke test:

```bash
docker compose exec airflow-scheduler airflow dags list-import-errors
docker compose exec airflow-scheduler airflow dags unpause hello
docker compose exec airflow-scheduler airflow dags trigger hello
docker compose exec airflow-scheduler airflow dags list-runs hello
```

`list-import-errors` prints `No data found` when every DAG file imports. The
hello run reaches `success` within a minute.

### Stop and reset

```bash
docker compose down      # keeps the postgres-db-volume (run history)
docker compose down -v   # deletes it (full reset)
```

### Secrets and overrides

Local development needs no `.env`; every value in `docker-compose.yaml` has a
dev default. On the VM (and any shared machine), before the first `up`:

```bash
cp .env.example .env
# set AIRFLOW_UID=$(id -u), POSTGRES_PASSWORD, the admin user and password,
# AIRFLOW__API_AUTH__JWT_SECRET, AIRFLOW__API__SECRET_KEY and FERNET_KEY
openssl rand -hex 32   # one value per secret
chmod 600 .env
```

Postgres and the admin user keep their first password until
`docker compose down -v`. The JWT secret, API secret key and Fernet key must
be identical in every container; the shared environment block in
`docker-compose.yaml` takes care of that, so set them only in `.env`.

### Troubleshooting

- **Docker Desktop quits a few seconds after it starts (Windows).** Podman
  Desktop also claims the `\\.\pipe\docker_engine` pipe, and Docker Desktop
  crashes when the pipe is taken. Quit Podman Desktop (system tray), then
  start Docker Desktop. Turn off Podman Desktop's start-on-login to keep it
  from happening after a reboot.
- **Every compose command fails with "unexpected character" in `.env`.** The
  file was written as UTF-16 by Windows PowerShell 5.1 `>` or `Out-File`.
  Recreate it with `Copy-Item .env.example .env` or an editor saving UTF-8.
- **Port 8080 is busy.** Set `AIRFLOW_HOST_PORT` in `.env`.
- **A DAG does not show up.** Run `airflow dags list-import-errors` (above).
  New files are picked up within 30 seconds.

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
