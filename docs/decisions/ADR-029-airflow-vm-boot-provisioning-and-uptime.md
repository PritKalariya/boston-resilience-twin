# ADR-029: airflow-vm boot provisioning and uptime

- Status: accepted; supersedes ADR-010's "runs 24/7 from Nov 1" line; amends ADR-023's mount layout and the root-owned `.env` and checkout in ADR-016 item 3; the "does not write `.env` or read Secret Manager" decision is amended by ADR-034
- Date: 2026-11-05
- Owner: PritKalariya
- Phase: deploy

## Context

ADR-016 recorded that Docker was installed on airflow-vm by hand, so a rebuilt VM needed the runbook's steps typed again. #60 puts them in a startup script. Writing the script forced three decisions that earlier ADRs left open or got wrong in practice:

- ADR-010 says the VM "runs 24/7 from Nov 1". That was a plan for a data pipeline that needed an always-on host. The project spec's revision log (2026-10-30) dropped the fixed 14-day unattended run. The VM now runs only for tests and the live run, and the live run's length is set at Deployment planning. The rest of the time the VM is stopped.
- ADR-016 item 3 has the VM's `.env` and checkout owned by root. A root-owned checkout makes `dvc push` fail from a container running as UID 50000, and `git pull` needs `sudo`, which leaves root-owned files in `.git`.
- ADR-023 mounts `dvc.yaml` and `dvc.lock` as single files. Docker binds a file by inode, and `git pull` replaces a changed file with a new inode, so the container keeps the pre-pull file until it is recreated.

The VM is on the default VPC with OS Login (ADR-015, ADR-018). Teammates hold `roles/compute.instanceAdmin.v1` (ADR-015; `team_project_roles` in `Deployment/terraform/iam.tf`), which can set instance metadata, so they could already run a script as root on the VM. A startup script adds no capability.

## Decision

**Uptime.** airflow-vm runs 24/7 only during the live run and for tests, and is stopped otherwise. Days it was stopped are caught up by the next full build (the DAG's default range): every download refetches what is new and every later step rebuilds from 2015. A date range writes a slice to data/slices/ and does not backfill. The VM is not kept up to avoid a gap. The DAG's `ingest_alert_history` task does not pass `--check-nws` (`pipeline_steps.py`, step `ingest_alert_history`), so it never reads an NWS alert snapshot and passes on a date that has none. Run by hand, `alert_history.py --check-nws` fails with `FreshnessError` when the folder has no snapshot or its newest one is older than the cutoff of the evening named by `--today`. It checks only that evening, from the newest snapshot, so it cannot be run for a past day. This replaces ADR-010's "runs 24/7 from Nov 1" line. Everything else in ADR-010 stands, including turning the VM off after the Expo on Dec 7.

**Boot provisioning.** `Deployment/terraform/startup.sh` is set as the instance's `startup-script` metadata key (not `metadata_startup_script`, which forces a replacement when it changes). GCE runs it as root on every boot, so each step does nothing when it is already done:

1. Before installing anything, run `dpkg --configure -a` (a stop during an earlier first boot can leave packages half installed), retrying for up to 10 minutes while automatic updates hold dpkg's lock. Then install Docker Engine and the compose plugin from Docker's apt repo, and git, only if missing.
2. Create the system user `airflow` with UID 50000 and no login shell, unless it exists. If UID 50000 belongs to another name, it exits non-zero.
3. Clone `main` as `airflow` into `/opt/boston-resilience-twin.partial` (first removing a leftover one from an interrupted run), then move it to `/opt/boston-resilience-twin`, if there is no `.git` there. A clone killed part way leaves no `.git` at the final path. If a checkout exists it touches nothing, and if the directory exists but is not a git repository it exits non-zero and does not delete anything.

It does not write `.env`, fetch secrets or run `docker compose up`. Those stay runbook steps until #64.

**B. Checkout owner.** The host user `airflow` (UID 50000) owns the clone. The VM's `.env` sets `AIRFLOW_UID=50000`, so the containers run as the owner of the checkout. Operators run git as `sudo -u airflow git ...` and docker as `sudo docker ...`. Host ownership is what keeps `Data-Pipeline/data` and `.dvc` writable from the containers. It does not make git trust `/opt/airflow/repo` inside a container, because Docker creates that directory as root there, on laptops and on the VM alike. So `docker-compose.yaml` sets `safe.directory` for it through the shared Airflow environment (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0`), and the `test` service. No image change.

**C. Directory mount.** `Data-Pipeline/` is mounted read-only as a directory at `/opt/airflow/repo/Data-Pipeline` (ADR-023's path), so a `git pull` reaches the containers without recreating them. `Data-Pipeline/data` is the only path under it that tasks write, and it is a read-write mount nested inside. The DAG mounts at `/opt/airflow/dags/<component>` (ADR-017) and the other mounts are unchanged.

## Alternatives considered

Uptime:

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **24/7 only for the live run and tests (chosen)** | About $1 a day while running (e2-medium at about $0.04 an hour, with its ephemeral IP), plus about $3 a month for the disk either way | Start and stop by hand; catch up gaps with a full build | A day the VM was stopped has no run. Days it was stopped are caught up by the next full build (the DAG's default range): every download refetches what is new and every later step rebuilds from 2015. A date range writes a slice to data/slices/ and does not backfill. The NWS snapshots (`nws.py forecasts` and `nws.py alerts`) are the exception: the NWS API keeps no archive, so a stopped day loses them permanently. No model input reads them. `nws_gridpoint_forecast` (`feature_catalog.csv` line 60) is `excluded` and has no history. The alert snapshots are read only by `alert_history.py --check-nws`, the freshness check on the evening pull behind `alert_snow_ice_count` through `alert_flooding_warning` (lines 29 to 34, used in the model). Those values come from the IEM archive, which can be pulled for any past day, so what a stopped day loses is that check's evidence for the day, not a feature value |
| 24/7 from Nov 1 (ADR-010) | About $31 a month, about $38 from Nov 1 to Dec 7 | None | Pays for five weeks of an idle VM before the live run starts |

B, who owns the checkout:

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Host user `airflow`, UID 50000 (chosen)** | $0 | A `useradd` in the script; `sudo -u airflow` for git | The UID is fixed. If something else holds 50000 the script stops with a message. A checkout made by root before this change stays root-owned, since the script never changes an existing one, so that VM is replaced |
| Root owns the checkout, git `safe.directory` set (ADR-016 item 3) | $0 | A git config line per user | `git pull` as root leaves root-owned files in `.git`, and `dvc push` from a container running as 50000 cannot write `.dvc` or `Data-Pipeline/data` |
| `AIRFLOW_UID` set to the operator's UID | $0 | None | Differs per person, and OS Login UIDs are large and arbitrary |
| Container runs as root | $0 | None | Root in the container writes root-owned data and logs onto the host disk, and the compose file's `user:` line says not to |

C, how `dvc.yaml` and `dvc.lock` reach the containers:

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Read-only `Data-Pipeline/` directory mount with a nested read-write `data` mount (chosen)** | $0 | Two mount lines replace five | A path writable today must be writable here. The nested mount's source must exist in a fresh clone, or Docker creates it as root. `Data-Pipeline/data` is tracked by `.gitkeep`. DVC cannot find its repo when run from inside `Data-Pipeline/`, because that directory is now a mount point. `dvc push` runs from the repo root and is unaffected |
| Keep the single-file mounts and recreate the containers after every `git pull` | $0 | One extra command per update | A forgotten recreate leaves `push_data` pushing the pre-pull `dvc.lock`, with no error |
| Mount the repo root read-only | $0 | One line | The VM's `.env`, with every Airflow secret, would be readable inside every container, and so would every other component's folder |
| Copy the files into the image | Image rebuild per change | Highest | The image rebuilds for each `dvc.lock` change, and `dvc.lock` changes with every data version |

## Consequences

- A VM from `terraform apply` reaches a healthy stack after only the runbook's `.env` and `up` steps. The first boot spends a few minutes in apt, and the script waits up to 10 minutes for apt's lock.
- Adding the key to the existing instance is an in-place update (`0 to add, 1 to change, 0 to destroy`) that does not run until the next boot. The VM made on 2026-10-18 has a root-owned checkout, so it is replaced (`terraform apply -replace`) when #60 is tested. Replacing loses Postgres run history, not data in GCS (ADR-016). The `vm_admins` sudo grants are recreated in the same apply.
- Editing `startup.sh` later is also an in-place update. Rerun it with `sudo google_metadata_script_runner startup`.
- ADR-016's "root-owned `.env`" is now an `.env` owned by `airflow`, mode 600. ADR-016 gets a status-line note only. Its note that Docker was installed by hand is also no longer true.
- A failed script does not stop the VM or the containers (`restart: unless-stopped`). It logs to the journal with a `brt-startup:` prefix, and the runbook says how to read that log.
- Tasks under `Data-Pipeline/` outside `data` cannot write, as before. Anything new that a task must write needs its own nested read-write mount, or must write under `data`.
- ADR-023's other decisions stand: the paths, `PYTHONPATH`, and the `data` mount are unchanged. Only the separate `scripts`, `config`, `dvc.yaml` and `dvc.lock` mounts are replaced by the directory mount.
- ADR-010's cost line (the VM takes about $42 of $50) no longer holds: running only for the live run and tests costs about $1 for each day it runs, plus about $3 a month for the disk. The total depends on the live run's length, which is set at Deployment planning.

## Revisit when

- The startup script needs to write `.env` or read Secret Manager (#64). That adds a secret to a script that runs as root on every boot.
- A second component's `dags/` or scripts need their own writable path, so the nested mounts multiply.
- The live run is extended or moved so the 24/7 window changes, or a missed day turns out not to be caught up by the next full build.
- Something other than `airflow` needs UID 50000 on the VM image.
- The base image moves off Ubuntu or Debian: the script stops on a distro it does not know.
