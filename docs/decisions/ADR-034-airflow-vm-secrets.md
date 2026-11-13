# ADR-034: airflow-vm's Airflow secrets live in Secret Manager and the startup script renders them into .env at every boot

- Status: accepted; amends ADR-029 (the startup script now writes `.env` and reads Secret Manager) and ADR-016 (its consequence that the VM runs on the compose dev defaults, and the "Revisit" note that W4-3 moves them)
- Date: 2026-11-13
- Owner: PritKalariya
- Phase: deploy

## Context

ADR-016 let airflow-vm run on the compose file's development defaults for every secret: the `airflow`/`airflow` admin login, `POSTGRES_PASSWORD=airflow`, a dev-only JWT secret and API secret key, and an empty Fernet key. It called that acceptable only while no credential is stored in Airflow, and promised that #64 would move them to Secret Manager before the first stored credential or Nov 1. ADR-029 gave the VM a startup script that installs Docker and clones the repo, and left `.env` as a hand-run runbook step (#132), with the three API keys copied in by hand. ADR-029's "Revisit when" named this change: the script writes `.env` and reads Secret Manager, which puts a secret handler in a script that runs as root on every boot.

The VM already holds run history in Postgres, so the change has to work on a live volume. The Postgres image reads `POSTGRES_PASSWORD` only when it creates an empty data directory, and Airflow's FAB auth manager (`AIRFLOW__CORE__AUTH_MANAGER` in `docker-compose.yaml`) reads `_AIRFLOW_WWW_USER_PASSWORD` only when it first creates the admin user, so a new `.env` alone changes neither on the existing VM.

The MLflow service needs no secret on the VM: it uses a SQLite file and a folder, has no password, and is not started on airflow-vm (profile `mlflow`, #61). The Slack webhook (#34) is a credential that ADR-019 said needs its writers decided explicitly before its slot exists.

## Decision

**Slots.** Terraform creates six new empty slots in project 1, automatic replication, no versions: `airflow-admin-password`, `airflow-postgres-password`, `airflow-jwt-secret`, `airflow-api-secret-key`, `airflow-fernet-key` and `slack-alert-webhook`. They sit in the same `for_each` resource as the three API key slots, keyed by id, so adding a slot never moves another. Values are added by hand with `gcloud secrets versions add`, never through Terraform.

**Accessors.** `airflow-vm-sa` gets `roles/secretmanager.secretAccessor` on every slot, at secret level, never project level. It already held it on `noaa-cdo-token`, `census-api-key` and `nws-user-agent` (the existing grant loops over every slot), so only the six new grants are added. Team members keep accessor on the three API keys only. Nobody but `airflow-vm-sa` (and the project owner) reads the five `airflow-*` slots or the webhook. `roles/secretmanager.secretAccessor` has only `secretmanager.versions.access`, not `secretmanager.versions.list`, and the render must tell "no version" from a failed read on the optional slots by listing their versions (`gcloud secrets versions list <name> --filter=state=ENABLED --limit=1`). So `airflow-vm-sa` also gets `roles/secretmanager.viewer` on those four slots (the three API keys and the webhook), at secret level, never project level. Viewer adds read of each slot's metadata and IAM policy and of its version list (ids and states, never values). The five required slots get no viewer grant: any failed read there stops the render, so they are never listed. A slot that does not exist yet makes the listing fail, so the render stops instead of writing the key empty.

**Writer scoping.** The existing `secret_writers` (ADR-019) get `roles/secretmanager.secretVersionAdder` on `slack-alert-webhook`, and keep it on the three API keys. They get no grant on the five `airflow-*` slots, so the project owner generates and adds those. The writers grant is now built from an explicit list (`local.writer_secret_ids`: the API keys plus the webhook) instead of every slot, so a future slot gets no writer by default. The existing grants keep their keys: no destroy or replace. This is the decision ADR-019's last "Revisit when" asked for about the Slack webhook.

**Boot render.** After its existing provisioning, `startup.sh` renders `/opt/boston-resilience-twin/.env` on every boot, whole, never edited in place. It contains `AIRFLOW_UID=50000` and each secret under the variable name `docker-compose.yaml` already reads (`POSTGRES_PASSWORD`, `_AIRFLOW_WWW_USER_PASSWORD`, `AIRFLOW__API_AUTH__JWT_SECRET`, `AIRFLOW__API__SECRET_KEY`, `FERNET_KEY`, `NOAA_CDO_TOKEN`, `CENSUS_API_KEY`, `NWS_USER_AGENT`, `SLACK_ALERT_WEBHOOK`). It fetches with `gcloud secrets versions access latest`, the project passed explicitly, trailing newlines stripped.

- The five `airflow-*` secrets are required. If any is unreadable, empty or cannot be written safely (a single quote or line break; the Postgres password must be letters and digits because it sits in a connection URL; the Fernet key must be one or more comma-separated 44-character keys), the existing `.env` is not touched, the log names the secret and never its value, and the script exits non-zero.
- `noaa-cdo-token`, `census-api-key`, `nws-user-agent` and `slack-alert-webhook` are optional. A secret with no version (the read failed and `gcloud secrets versions list <name> --filter=state=ENABLED --limit=1` returned nothing) writes its key empty and logs a warning naming it, because the tasks that need a key already fail loudly without it. Any other read failure on an optional secret fails the render like a required one, so a network error never blanks a key that has a value.
- It writes a temp file in the same directory under `umask 077`, sets owner `50000:50000` and mode 600, and renames it over `.env`. No `set -x`, no value is echoed, nothing secret reaches the serial console.
- It never starts or restarts compose. Containers read `.env` when they are created, so every change ends with `sudo docker compose up -d --wait`. The file is readable only by UID 50000 and root, so the runbook runs compose with `sudo` (root reads it) and never loosens the mode.
- The fetch command, the target path, the owner and the project are overridable by environment variables, and `BRT_RENDER_ONLY=1` skips the provisioning, so a stub fetcher drives the render on a laptop with no root, Docker or cloud call.

**Fernet-encrypted storage, not the Google Secret Manager secrets backend.** Airflow stores connections and variables in Postgres encrypted with the Fernet key, which is one of the five secrets. Airflow's own Secret Manager secrets backend is not adopted: no connection or variable exists yet, and the pipeline reads its keys from the environment. The alerts module's masking list gains the four Airflow secrets as the containers see them.

**Migration of the existing VM keeps run history.** The Postgres role is changed in place with `ALTER USER airflow PASSWORD ...`, the SQL piped to `psql` through stdin so the password is on no command line, then the startup script is rerun, the stack recreated, the admin password reset with `airflow users reset-password` (value on stdin), and `airflow rotate-fernet-key` run. No fresh volume. The order, the checks and the rotation of each secret are in the runbook ("Airflow on the VM", "Secrets on the VM"). The password is held in a shell variable and written by the shell builtin `printf`, so it is on no process's command line, and an empty or non-alphanumeric value is refused first, because `ALTER USER ... PASSWORD ''` would remove the password.

**Laptops are unchanged.** `docker-compose.yaml` keeps its dev defaults and `.env.example` its blanks. Only their comments change, to say the VM's values come from Secret Manager at boot.

**Terraform PRs are plan-only.** The PR carries the plan: 17 to add (6 slots, 6 `airflow-vm-sa` accessor grants, 4 `airflow-vm-sa` viewer grants on the optional slots, 1 writer grant), 1 to change (the instance's `startup-script` metadata, in place), 0 to destroy. The owner applies from `main` after merge.

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Boot render of `.env` from Secret Manager; Fernet over the Airflow secrets backend (chosen)** | About $0.06 per active secret version a month, so under $1 for the ten slots (estimate, check the price list) | One script section, six slots, a migration runbook | A failed required fetch leaves the old `.env` and no `done` line; the VM and running containers are unaffected. Boot never blocks on the stack |
| Keep the dev defaults on the VM (ADR-016) | $0 | None | The admin login is `airflow`/`airflow`, the JWT secret and API key are in a public repo, stored connections are unencrypted. Safe only while nothing is stored in Airflow |
| Hand-fill `.env` from Secret Manager (#132 step 2) | $0 | A long command on every rebuilt VM | A rebuilt VM has no `.env` until a person runs it, and the Airflow secrets stay on the defaults |
| Airflow Google Secret Manager secrets backend | Secret Manager access calls (low) | A provider config, a naming convention, a service account role per prefix | Adds a network dependency to every connection lookup; solves a problem the project does not have yet (no connections or variables) |
| Fresh Postgres volume instead of `ALTER USER` | $0 | Lowest | Loses run history; the right option for a replaced VM, not for a live one |
| `.env` copied from a metadata attribute or a Terraform-managed value | $0 | Low | The secret lands in Terraform state and instance metadata, readable by anyone with `compute.instanceAdmin.v1` (ADR-015) |
| Match gcloud's `NOT_FOUND` text to detect "no version" | $0 | None | Also matches a mistyped slot, and breaks when gcloud rewords its error |
| Project-level accessor for `airflow-vm-sa` | $0 | Lowest | A future secret is readable by the VM without a decision |
| Writers on every slot (the old fan-out) | $0 | None | The writers could replace the Fernet key or the database password |

## Consequences

- airflow-vm no longer runs on any dev default. `airflow`/`airflow` stops working after the migration, which the runbook checks.
- The boot render is a secret handler that runs as root on every boot. It prints secret names only, reads through `airflow-vm-sa`, and its only effect is one file with mode 600. Anyone with `compute.instanceAdmin.v1` can already run a script as root on the VM (ADR-015, ADR-029), so it adds no capability; it does put the secrets on the VM's disk, readable by root and UID 50000.
- Reading the admin password to log in to the UI is limited to people with sudo on the VM (`vm_admins`), since team members hold no grant on that slot.
- A rotation of the Fernet key takes four steps; the others take one or two. Each ends with a recreate. Old versions must be destroyed after each rotation: disabled ones still bill.
- Terraform: the new startup script is an in-place update of the instance (`~ update in-place`, no replacement), taking effect at the next boot or with `sudo google_metadata_script_runner startup`.
- A new VM from `terraform apply -replace` needs the five values added before its first boot, or its `.env` is not written and the stack has no secrets until they are.
- Fernet rotation holds two keys in one version (`new,old`) for a short window. The alerts masking replaces each comma-separated part of the Fernet key as well as the whole value (no other credential is split), so a log line that quotes only one of the two keys is masked.
- The VM's `.env` is no longer a place for local overrides such as `LEGACY_PARALLEL`. A VM-only override needs a line in the render, or a change to the compose default.

## Revisit when

- The first credential has to live as an Airflow connection or variable (a database, an SMTP password, a Gmail app password): adopt the Google Secret Manager secrets backend then, and decide who reads each such secret.
- A second VM, a different image or a CI job needs the same secrets: move the render to a small script both use.
- A required secret should survive its slot being emptied: add a last-known-good copy outside `.env`, or fail the VM start instead of the script.
- The Airflow or auth manager changes (`FabAuthManager` is replaced): the admin reset command in the runbook changes with it.
- Someone other than the owner must add the `airflow-*` values: decide their writers explicitly, as ADR-019 does for the API keys.
