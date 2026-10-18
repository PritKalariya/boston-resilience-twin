# ADR-016: Airflow on airflow-vm runs LocalExecutor with a Postgres container, reached only by SSH tunnel

- Status: accepted
- Date: 2026-10-18
- Owner: PritKalariya
- Phase: deploy

## Context

ADR-010 puts Airflow in Docker on one e2-medium VM (2 shared vCPUs, 4 GB, no swap), stopped when idle in October and on 24/7 from Nov 1. The project spec left the executor and the metadata DB open. The local stack from W1-2 (#55) already runs LocalExecutor with Postgres 16. Each member's project is capped at 50 dollars of credits (ADR-009). The VM sits on the default VPC: SSH is open to 0.0.0.0/0, and `default-allow-internal` opens every port to 10.128.0.0/9.

## Decision

1. LocalExecutor. Tasks run as subprocesses inside the scheduler container.
2. The metadata DB is Postgres 16 in a container, on a named volume on the VM's boot disk.
3. Laptops and the VM run the same `docker-compose.yaml`. VM-only settings live in a root-owned `.env` (mode 600) in `/opt/boston-resilience-twin`, which tracks `main`. Airflow runs as `AIRFLOW_UID=50000`, not as any one person.
4. Every port the stack publishes binds `127.0.0.1`. UIs are reached only through `gcloud compute ssh ... -L`, and no firewall rule opens a UI port. This applies to MLflow and Evidently when they join the VM.
5. No triggerer service. DAGs use no deferrable operators; sensors use `mode="reschedule"`.

## Alternatives considered

| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **LocalExecutor, Postgres container (chosen)** | $0 beyond the VM | Already running on laptops and the VM | A memory-heavy task shares the scheduler's container and the VM's 4 GB; with no swap, the OOM killer can take the scheduler. Losing the boot disk loses run history, not data (GCS holds data) |
| CeleryExecutor, Redis, worker | $0; about 300 to 500 MB more RAM (estimate) | Two more services to run and watch | A crashed worker is isolated, but an OOM on one 4 GB VM still hits everything; Redis adds a failure point |
| LocalExecutor, Cloud SQL Postgres | About $9/month on top of the VM (smallest shared-core tier, unverified) | Terraform, a connector or private IP, a password in Secret Manager | Run history survives VM loss; adds a network dependency |
| KubernetesExecutor | GKE nodes, over the 50-dollar budget | Cluster plus Helm chart | Pod per task, strongest isolation |
| SQLite metadata DB | $0 | Least | Airflow supports it for development only |
| Public UI port with Airflow login only | $0 | One firewall rule | The login page and API face the internet; the password is the only control |

## Consequences

- Measured on the VM on 2026-10-18: the containers use 837 MiB idle (scheduler 284, API server 268, dag-processor 202, Postgres 83 MiB), leaving 2,504 MiB available.
- First start took 9m47s, about 5 minutes of it pulling the Airflow image. After a VM start, the stack is healthy in about 1 minute without running `up`.
- Concurrency is limited only by `core.parallelism` (default 32). Cap it to fit memory before heavier DAGs land.
- Stop the VM, never the containers. `unless-stopped` brings containers back on boot only if nobody stopped them by hand.
- Postgres run history lives on the boot disk. Replacing the VM or running `terraform destroy` wipes it, so anything that must survive goes to GCS.
- The UI's real access control is OS Login (IAM). The compose default login stays for October, because every teammate with OS Login also has `compute.instanceAdmin.v1`, which can already reach root (ADR-015).
- The UI's health panel shows Triggerer in red. That is expected.
- Docker was installed by hand from Docker's apt repo (see the runbook), not by Terraform.

## Revisit when

- A DAG needs a deferrable operator: add the triggerer service (about 200 MiB, similar to the dag-processor).
- A real DAG run pushes memory above about 3 GB or a task is OOM-killed: cap parallelism, add swap, or resize (e2-standard-2 with 8 GB is about $49/month, over one member's budget).
- Run history must survive VM loss: move to Cloud SQL.
- Airflow needs a second machine: CeleryExecutor.
- Someone needs a UI without SSH: IAP TCP forwarding, never a public port.
