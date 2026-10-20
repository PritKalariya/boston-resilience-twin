# ADR-010: Airflow in Docker on one e2-medium VM

- Status: accepted; details amended by ADR-015 (infrastructure) and ADR-016 (executor, database, access)
- Date: 2026-10-04
- Owner: kalariya.p
- Phase: deploy

## Context

The course lists Airflow as required software. The pipeline needs an evening forecast DAG, an event DAG every 10 to 15 minutes during weather alerts, and a 14-day unattended run from Nov 16. Before Nov 1 it needs a host only part of the time. Project 1 pays out of $50 (ADR-009). Later details live in ADR-015 and ADR-016.

## Decision

- Airflow runs in Docker on one e2-medium VM (2 shared vCPUs, 4 GB) in project 1.
- The VM is created in week 1 with Terraform.
- It is stopped when idle in October, runs 24/7 from Nov 1, and is turned off after the Expo on Dec 7.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Cloud Composer, smallest environment | About $300 to $400 a month, billed while idle | Low: managed | Uses up a $50 project in days |
| **Docker on an e2-medium VM (chosen)** | About $31 a month running (VM $24.46, disk $3, ephemeral IP $3.65); $3 stopped | We own patching, restarts, and disk | One VM: if it dies, DAGs stop until it restarts |
| Docker on an e2-small VM | About $19 a month running (VM $12.23, same disk and IP) | Same as above | 2 GB is below the 4 GB Airflow's Docker setup asks for |
| Laptops only | $0 | Lowest | No 24/7 host for the evening forecast or the 14-day run |
| Cloud Run jobs plus Cloud Scheduler | Cents a month | Scheduling rebuilt outside Airflow | Not Airflow, which the course requires |

## Consequences

- Nov 1 to Dec 7 costs about $38, and October disk and dev hours add a few dollars. The VM takes about $42 of project 1's $50, above the scoping estimate of $35.
- Whoever starts the VM stops it. A running idle VM costs about $1 a day.
- No standby: if the VM dies during the 14-day run, recovery is a restart.
- MLflow tracking, the grading job, and Evidently share the VM's memory with Airflow.

## Revisit when

- Project 1's projected spend passes $40.
- VM memory stays above 80% (about 3,100 of 3,908 MiB) during DAG runs. The idle stack measured 837 MiB (ADR-016); the peak under a real DAG is not measured yet.
- The live run needs more uptime than one VM can give.
