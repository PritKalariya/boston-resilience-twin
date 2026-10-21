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
echo "AIRFLOW_UID=$(id -u)" > .env   # Linux only (the VM too), once per clone
docker compose up -d --wait
docker compose ps -a
curl -fsS http://127.0.0.1:8080/api/v2/monitor/health
```

Without `AIRFLOW_UID`, Linux log files end up owned by uid 50000 (see Stop
and reset). macOS and Windows need no `.env`.

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

The containers restart whenever Docker starts until `docker compose down`.
`down -v` deletes the database volume only; task logs in `Data-Pipeline/logs/`
are host files and stay.

On Linux, if the stack ever ran without `AIRFLOW_UID`, those log files belong
to uid 50000 and deleting them fails with "Permission denied". Take them back:

```bash
sudo chown -R "$(id -u):$(id -g)" Data-Pipeline/logs
```

### Overrides and secrets

`.env` is for local development only. macOS and Windows need none; Linux
needs `AIRFLOW_UID` (above). Other local overrides use the names in
`.env.example`. Postgres and the admin user keep their first password until
`docker compose down -v`.

The VM does not use `.env` for secrets: they come from Secret Manager
(ADR-015), set up in #20. However they are supplied, the JWT secret, API
secret key and Fernet key must be identical in every container; the shared
environment block in `docker-compose.yaml` passes one value to all of them.

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
- **"Permission denied" on `Data-Pipeline/logs` (Linux).** The stack ran
  without `AIRFLOW_UID`. Run the `sudo chown` under Stop and reset, then
  write `.env` before the next start.
- **A DAG does not show up.** Run `airflow dags list-import-errors` (above).
  New files are picked up within 30 seconds.

## Surge baseline re-check (ADR-005)

ADR-005 builds surge baselines from the same weeks of the previous 3 years,
both 311 systems combined. Rerun the check when any of these happens:

- the first new-system snow of 25 mm or more at Logan;
- a `docs/data/category-map.csv` PR that touches a storm group;
- the live quiet-day surge rate stays above the replay maximum for 30 days.

```bash
python Data-Pipeline/scripts/legacy_311.py
python Data-Pipeline/scripts/new_311.py
python Data-Pipeline/scripts/map_311.py --change-points Data-Pipeline/data/interim/change-points.csv
python Data-Pipeline/scripts/live_baselines.py
```

`map_311.py` writes the switch dates to the gitignored data folder here, so
the committed `docs/data/change-points.csv` only changes in a reviewed PR.
For snow, compare snow-day counts with the legacy range and look for
unmapped or non-snow categories that jump on snow days; open a category-map
PR if so, and update ADR-005.

## Static layers

`Data-Pipeline/config/static_layers.csv` lists each static layer: file name, source URL, publisher, vintage, license, and the key column that must be present, unique and non-blank. To add or replace a layer, change a row there in a reviewed PR.

```bash
python Data-Pipeline/scripts/static_layers.py --download   # fresh clone: fetch missing files, then load
python Data-Pipeline/scripts/static_layers.py              # files already in Data-Pipeline/data/raw/static/
python Data-Pipeline/scripts/static_layers.py --refresh    # fetch every file again (source updated)
```

Raw files go to `Data-Pipeline/data/raw/static/` and the loaded layers to `Data-Pipeline/data/interim/static/` (vectors as GeoParquet in EPSG:4326), both gitignored. The run rewrites `docs/data/static-layers.csv` with the source CRS, row count, retrieval date and SHA-256 of each file; commit it with the change. A changed hash after `--refresh` means the city published a new version, so check the row counts before using it. The run stops on a missing, empty or unreadable file, a missing or duplicate key, or a layer that falls outside Boston after reprojection (usually a wrong CRS).

## Project 1 infrastructure (Terraform)

Config lives in `Deployment/terraform/`. It covers project 1 only (ADR-009):
the Airflow VM, the data bucket, the VM service account, two empty secret
slots, the project 1 APIs, and IAM. Nothing for Cloud Run, Artifact Registry,
Vertex, or Gemini.

State is in the GCS bucket `boston-resilience-twin-tfstate` under the prefix
`project1`. That bucket is not managed by Terraform.

The data bucket `boston-resilience-twin-data` has object versioning on, so an
overwritten raw pull is kept as a noncurrent version. A lifecycle rule deletes
noncurrent versions 30 days after they become noncurrent, which bounds the cost
of keeping every overwrite. Live objects are never deleted by the rule, so a
mistaken overwrite can still be rolled back inside that 30-day window. Confirm
the rule on the bucket with:

```bash
gcloud storage buckets describe gs://boston-resilience-twin-data \
  --format="default(lifecycle_config)"
```

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

### Adding a secret value

Terraform creates the secret slots empty. Values are added by hand and never
go into a `.tf` file, a tfvars file, chat, the repo, or a ticket.

Who can: the members listed in the `secret_writers` tfvars variable. They hold
`roles/secretmanager.secretVersionAdder` on `noaa-cdo-token` and
`census-api-key` only, which is the permission to add a new version. It is not
project wide and it does not let them read the values back; reading is
`roles/secretmanager.secretAccessor`, which every team member already has.

```bash
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets versions add <secret> --data-file=-; unset VALUE
```

Run it, paste the value at the blank prompt, and press Enter. `read -s` hides
the paste, and `printf '%s'` sends it without a trailing newline. gcloud stores
stdin byte for byte, so a trailing newline would become part of the key and the
NOAA and Census APIs would reject it. The value never appears in shell history,
and `unset` clears it from the shell even if the gcloud call fails.

To confirm a stored value has no trailing newline (prints its last character):

```bash
gcloud secrets versions access latest --secret=<secret> | tail -c 1 | od -An -c
```

If it prints `\n`, add a new version with the command above and disable the
old one. If a value is ever exposed, add a new version with a fresh value and
disable the old version.

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

The data bucket has `force_destroy = false`, so a destroy fails while objects
are still in it. Empty it first, deliberately, only when the data is no longer
needed. The 30-day lifecycle rule clears old versions, not live ones, so this
step is still required:

```bash
gcloud storage rm -r --all-versions gs://boston-resilience-twin-data/**
```

Then destroy all Project 1 resources:

```bash
cd Deployment/terraform
terraform destroy -var-file=terraform.tfvars
```

**The state bucket is not destroyed by Terraform.** `boston-resilience-twin-tfstate`
is outside this config on purpose, so a destroy cannot delete the state it is
reading. Remove it by hand, last, after the destroy has finished:

```bash
gcloud storage rm -r --all-versions gs://boston-resilience-twin-tfstate
```

## Airflow on the VM

How Airflow runs on `airflow-vm`, and why: ADR-016. The VM runs the same `docker-compose.yaml` as the local stack, checked out from `main` at `/opt/boston-resilience-twin` and owned by root. Run every command on the VM with `sudo` unless it says laptop. Starting, stopping, and SSH are under "Stop the VM when idle" above. Nobody joins the `docker` group: it is root-equivalent, and `sudo` keeps the operator list equal to ADR-018's.

### First-time setup (new or rebuilt VM only)

Done on 2026-10-18 on the current VM. Docker is not in Terraform yet, so a rebuilt VM needs these steps again.

Install Docker Engine and the compose plugin from Docker's apt repo:

```bash
sudo bash -euo pipefail <<'EOF'
apt-get update -q
apt-get install -y -q ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl is-enabled docker
EOF
```

If apt reports `Could not get lock`, automatic updates are still running from boot. Wait two minutes and rerun; every line is safe to repeat.

Clone `main` and write the VM's `.env`:

```bash
sudo bash -euo pipefail <<'EOF'
git clone -q https://github.com/PritKalariya/boston-resilience-twin.git /opt/boston-resilience-twin
cd /opt/boston-resilience-twin
printf 'AIRFLOW_UID=50000\n' > .env
chmod 600 .env
EOF
```

`AIRFLOW_UID=50000` is deliberate. The compose file's comment says to use `id -u` on Linux, but under `sudo` that returns 0, which would run Airflow as root.

Start the stack:

```bash
cd /opt/boston-resilience-twin && sudo docker compose up -d --wait
```

The first start takes about 10 minutes on an e2-medium, half of it pulling the Airflow image. It is not hung.

### After a VM start

Nothing to do. The services use `restart: unless-stopped`, so Docker brings them back at boot, and the stack is healthy about 1 minute after the VM starts. Check with:

```bash
cd /opt/boston-resilience-twin && sudo docker compose ps
```

All four services (postgres, airflow-apiserver, airflow-scheduler, airflow-dag-processor) should show `(healthy)`.

**Stop the VM, never the containers.** Running `docker compose stop` or `down` before a VM stop marks the containers as stopped by hand, and they stay down after the next start. If that happens, run `sudo docker compose up -d --wait`.

### Open the UI (laptop)

The API server listens on the VM's `127.0.0.1:8080` only, and no firewall rule opens 8080. Open a tunnel and leave its terminal running:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b -- -N -L 18080:localhost:8080
```

Then browse to `http://localhost:18080` and log in with the compose defaults, `airflow` / `airflow` (ADR-016 explains why that is acceptable). Port 18080 avoids your local stack's 8080. Ctrl+C closes the tunnel. Health without a browser: `curl -s http://localhost:18080/api/v2/monitor/health`.

### Update after a merge to main

```bash
cd /opt/boston-resilience-twin && sudo git pull --ff-only && sudo docker compose up -d --wait
```

The VM runs `main` only. Test branches on your laptop.

### Run a DAG from the CLI

```bash
cd /opt/boston-resilience-twin
sudo docker compose exec airflow-scheduler airflow dags unpause <dag_id>
sudo docker compose exec airflow-scheduler airflow dags trigger <dag_id>
sudo docker compose exec airflow-scheduler airflow dags list-runs <dag_id>
```

New DAGs start paused (`DAGS_ARE_PAUSED_AT_CREATION`), and a run triggered on a paused DAG stays `queued`.

### Troubleshooting

- **Triggerer shows red in the UI's health panel.** Expected: the stack has no triggerer service (ADR-016).
- **SSH stalls after "Server accepts key".** Your laptop's ssh-agent has the key but it is locked. Run `ssh-add ~/.ssh/google_compute_engine` on the laptop, then retry.
- **A task is OOM-killed or the scheduler restarts.** With the stack idle, the VM has about 2.5 GB free and no swap. Check `sudo docker inspect -f '{{.Name}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' $(sudo docker compose ps -aq)` and `free -m`.
- **`gcloud compute ssh` offers to create an SSH key.** You ran it on the VM itself. The prompt shows where you are: `@airflow-vm` is the VM.
