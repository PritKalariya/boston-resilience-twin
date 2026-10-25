# Runbook

Operational steps for the Boston Resilience Twin. Update this file in the PR
that changes an operational step.

## Local Airflow stack (Docker Compose)

`docker-compose.yaml` at the repo root runs Airflow 3.3.2 with LocalExecutor
and a Postgres 16 metadata database: `postgres`, a one-shot `airflow-init`,
`airflow-apiserver` (UI and API on 127.0.0.1:8080 only), `airflow-scheduler`
(also runs the tasks), and `airflow-dag-processor`. There is no triggerer: add
the official triggerer service back when the first deferrable operator lands,
since a deferred task waits forever without one.

Every Airflow service runs `brt-airflow:local`, built from `airflow.Dockerfile`
at the repo root: the `apache/airflow:3.3.2-python3.12` base image (ADR-013)
plus `Data-Pipeline/requirements.txt` installed under Airflow's constraints for
that image (ADR-022). The requirements file is the only thing copied in; DAG
and script code is bind-mounted (ADR-017).

### Rebuild the image

```bash
docker compose up -d --build --wait
```

Run it after any change to `Data-Pipeline/requirements.txt`, including one that
arrives with a `git pull`. DAG and script changes never need a rebuild. With
the requirements unchanged the build is fully cached and adds about a second,
so it is safe to use `--build` on every start.

### Start and check

```bash
echo "AIRFLOW_UID=$(id -u)" > .env   # Linux only (the VM too), once per clone
docker compose up -d --build --wait
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

### Run the data pipeline DAG

`Data-Pipeline/dags/daily_pipeline.py` chains ingestion, processing and
validation, one task per module (#22). `docker-compose.yaml` mounts the repo
under `/opt/airflow/repo` (scripts, config and `docs/data` read-only,
`Data-Pipeline/data` writable) and puts `Data-Pipeline/scripts` on
`PYTHONPATH`, so tasks import the scripts (ADR-017).

- Five tasks run scripts: `ingest_static_layers`, `parse_legacy_311`,
  `parse_new_311`, `combine_311` and `spatial_join`.
- The other 13 are placeholders. Each logs the issue that builds it
  (`PLANNED` in `Data-Pipeline/scripts/pipeline_steps.py`) and succeeds.
- Until #6 lands, the raw 311 files go in `Data-Pipeline/data/raw/311/` by
  hand.

The DAG runs only when triggered. It takes two params, both inclusive:
`start` (2015-01-01 or later) and `end` (empty: through the latest data).

```bash
docker compose exec airflow-scheduler airflow dags unpause daily_pipeline
# One year, into Data-Pipeline/data/slices/2025-01-01_2025-12-31/
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r slice-2025 -c '{"start": "2025-01-01", "end": "2025-12-31"}'
# Full build, into Data-Pipeline/data/interim/
docker compose exec airflow-scheduler airflow dags trigger daily_pipeline -r full-build
docker compose exec airflow-scheduler airflow dags list-runs daily_pipeline
docker compose exec airflow-scheduler airflow tasks states-for-dag-run daily_pipeline slice-2025
```

In the UI, Trigger opens a form with the two dates. A slice never overwrites
the full-build files that other scripts read; delete its folder in
`Data-Pipeline/data/slices/` when you are done. A slice keeps only its days,
in Boston local time (new-system times are already local, ADR-020): legacy
rows are cut right after parsing, and the combined table after combining.
The parsed new-system export stays whole, because combining needs every new
case to set the switch dates and to drop resubmitted legacy copies. So a
slice's `311_combined.parquet` holds the same rows as the full build has for
those days. A run fails at `check_range`
on a start before 2015, an end before the start, or an unknown conf key.
Each task's log, with everything its script printed, is in
`Data-Pipeline/logs/dag_id=daily_pipeline/run_id=<run>/task_id=<task>/`.
On a laptop (2026-10-21), a one-year slice took 40 seconds and the full
build about 2.5 minutes. The full build peaked at 3.4 GiB in the scheduler
container, with `map_311.py` before #68.

Logging and failures (#36):

- Every task logs one line with its run id, rows in and rows out, for example
  `run slice-2025 task combine_311: rows in 348,466, rows out 280,044 in 3.1 s`.
  Placeholders log 0 and 0, and `check_range` logs n/a. CSV inputs are
  counted as records, the way the parsers report `file_rows`
  (`legacy_311.count_records`), and parquet files from their metadata;
  `ingest_static_layers` counts only the layers in its config. Counting reads
  each raw CSV once more: about 1 s per yearly file in the container on a
  Windows laptop, 12 s for all 13 files.
- The `ingest_*` tasks retry twice. Airflow's exponential backoff adds a
  random spread, so the waits are about 5 to 10 minutes and then 10 to 15
  (capped). Every other task fails at once.
- A real step whose input file is missing or empty fails before its script
  runs (`InputError`), naming the file and how to get it. The DAG raises it
  as `AirflowFailException`, so even an `ingest_*` task fails at once instead
  of retrying. A script error is logged as `run <id> task <task> failed:
  <error>` and keeps its own type.
- Before a step runs, its outputs are deleted, so a file left by an earlier
  run cannot pass for new output. `ingest_static_layers` also deletes empty
  (0-byte) files in `Data-Pipeline/data/raw/static/`, so `--download` fetches
  them again.
- A task that fails for good writes `ALERT <dag>.<task> failed in run <id>
  (try <n>): <error>` to its own log (`Data-Pipeline/scripts/alerts.py`).
  Slack or email delivery comes with #34.

- **`ingest_static_layers` fails with `No module named 'geopandas'`.** The
  containers run an image built before the geo packages were added (ADR-022).
  Rebuild with `docker compose up -d --build --wait`.
- **Linux: "Permission denied" in `Data-Pipeline/data`.** The stack ran
  without `AIRFLOW_UID`, so `airflow-init` gave the folder to uid 50000. Set
  `AIRFLOW_UID` (Start and check), then take it back:
  `sudo chown -R "$(id -u):$(id -g)" Data-Pipeline/data`.

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
  New files are picked up within 30 seconds. A new component also needs a
  volume line for its `dags/` folder in `docker-compose.yaml`, mounted at
  `/opt/airflow/dags/<component>` in snake_case (ADR-017); without it, Airflow
  never sees the folder.
- **A DAG task fails with `ModuleNotFoundError` after a pull.** The image is
  stale: someone added a package to `Data-Pipeline/requirements.txt` and your
  containers still run the image built before it. Rebuild with
  `docker compose up -d --build --wait`.
- **DAGs still load from the old path after a pull.** A running stack keeps
  its old mounts until its containers are recreated. After pulling a change
  to `docker-compose.yaml`, such as the ADR-017 DAG mount, run
  `docker compose down`, then `docker compose up -d --build --wait`.

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
python Data-Pipeline/scripts/live_baselines.py --change-points Data-Pipeline/data/interim/change-points.csv
```

`map_311.py` writes the switch dates to the gitignored data folder here and
`live_baselines.py` reads them from there, so the committed
`docs/data/change-points.csv` only changes in a reviewed PR.
For snow, compare snow-day counts with the legacy range and look for
unmapped or non-snow categories that jump on snow days; open a category-map
PR if so, and update ADR-005.

## After-switch policy (ADR-021)

`map_311.py` flags legacy requests opened on or after their category's
first new-system day (`after_switch`). ADR-021 keeps them. The setting is
`after_switch_policy` in `Data-Pipeline/config/processing.csv`: `keep`
(default), `drop`, or `exclude_from_baselines` (counted on their day, left
out of baselines). Change it only in a reviewed PR that updates ADR-021,
regenerates counts and labels, and reports the change in totals.

Rerun the check after a new 311 export, after a category-map PR, and at the
first snow of the winter:

```bash
python Data-Pipeline/scripts/legacy_311.py
python Data-Pipeline/scripts/new_311.py
python Data-Pipeline/scripts/map_311.py --change-points Data-Pipeline/data/interim/change-points.csv
python Data-Pipeline/scripts/static_layers.py --download --manifest Data-Pipeline/data/interim/static-layers.csv
python Data-Pipeline/scripts/after_switch.py
```

The `--manifest` path keeps the committed `docs/data/static-layers.csv` as
it is. `after_switch.py` takes about 1.5 minutes and writes no files. Look
for a window where both systems logged many rows and the ratio under keep is
well above 1 (requests entered twice), and in winter for Snow Plowing rows
flagged `after_switch`.

## Static layers

`Data-Pipeline/config/static_layers.csv` lists each static layer: file name, source URL, publisher, vintage, license, and the key column that must be present, unique and non-blank. To add or replace a layer, change a row there in a reviewed PR.

```bash
python Data-Pipeline/scripts/static_layers.py --download   # fresh clone: fetch missing files, then load
python Data-Pipeline/scripts/static_layers.py              # files already in Data-Pipeline/data/raw/static/
python Data-Pipeline/scripts/static_layers.py --refresh    # fetch every file again (source updated)
```

Raw files go to `Data-Pipeline/data/raw/static/` and the loaded layers to `Data-Pipeline/data/interim/static/` (vectors as GeoParquet in EPSG:4326), both gitignored. The run rewrites `docs/data/static-layers.csv` with the source CRS, row count, retrieval date and SHA-256 of each file; commit it with the change. A changed hash after `--refresh` means the city published a new version, so check the row counts before using it. The run stops on a missing, empty or unreadable file, a missing or duplicate key, or a layer that falls outside Boston after reprojection (usually a wrong CRS).

## Spatial join (311 requests to tracts and neighborhoods)

`spatial_join.py` places every request in `311_combined.parquet` by its coordinates, in a 2020 tract and the ADR-004 neighborhood that holds it, and writes `Data-Pipeline/data/interim/311_located.parquet`. It needs the static layers loaded first.

```bash
python Data-Pipeline/scripts/spatial_join.py
```

The run adds `geoid20`, `unit` (the neighborhood a request is counted in) and `location`, and prints the share with no unit by year and system. `location` is `placed` or the reason there is no unit: `no_coordinates` (missing or not a number), `placeholder` (a default point listed in `Data-Pipeline/config/placeholder_locations.csv`, matched within about a metre), `outside_boston` (no 2020 tract) or `no_neighborhood` (tract 9815.02, which the city leaves out of every neighborhood). A latitude and longitude entered the wrong way round are swapped back and counted. If a new default point turns up (one spot holding thousands of requests with no neighborhood), add it to the placeholder file in a reviewed PR.

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

### Sudo on the VM

`roles/compute.osLogin`, which every team member has, is SSH without sudo.
Sudo is `roles/compute.osAdminLogin`, and it is held only by the members
listed in the `vm_admins` tfvars variable (the owner and Srikar). The grant is
on the `airflow-vm` instance, not on the project, so it does not follow to any
other VM that project 1 gains later.

Check whether you have it by running any root command over SSH:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b --command='sudo -n true && echo sudo ok'
```

To give someone sudo, add their `user:` address to `vm_admins` in
`terraform.tfvars` (one line) and apply:

```bash
cd Deployment/terraform
terraform plan -var-file=terraform.tfvars -out=tfplan
terraform apply tfplan
```

Removing the line and applying again takes it away. A member who has never
signed in to GCP fails the same way as the other IAM grants above.

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
cd /opt/boston-resilience-twin && sudo docker compose up -d --build --wait
```

The first start takes about 10 minutes on an e2-medium, half of it pulling the Airflow image. It is not hung.

### After a VM start

Nothing to do. The services use `restart: unless-stopped`, so Docker brings them back at boot, and the stack is healthy about 1 minute after the VM starts. Check with:

```bash
cd /opt/boston-resilience-twin && sudo docker compose ps
```

All four services (postgres, airflow-apiserver, airflow-scheduler, airflow-dag-processor) should show `(healthy)`.

**Stop the VM, never the containers.** Running `docker compose stop` or `down` before a VM stop marks the containers as stopped by hand, and they stay down after the next start. If that happens, run `sudo docker compose up -d --build --wait`.

### Open the UI (laptop)

The API server listens on the VM's `127.0.0.1:8080` only, and no firewall rule opens 8080. Open a tunnel and leave its terminal running:

```bash
gcloud compute ssh airflow-vm --zone=us-east1-b -- -N -L 18080:localhost:8080
```

Then browse to `http://localhost:18080` and log in with the compose defaults, `airflow` / `airflow` (ADR-016 explains why that is acceptable). Port 18080 avoids your local stack's 8080. Ctrl+C closes the tunnel. Health without a browser: `curl -s http://localhost:18080/api/v2/monitor/health`.

### Update after a merge to main

```bash
cd /opt/boston-resilience-twin && sudo git pull --ff-only && sudo docker compose up -d --build --wait
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
