# ADR-017: Components as top-level folders, one shared Airflow stack at the root

- Status: accepted
- Date: 2026-10-17
- Owner: PritKalariya
- Phase: setup

## Context

The Data Pipeline assignment's example layout is one folder holding `dags/`, `data/`, `scripts/`, `tests/`, `logs/`, `dvc.yaml`, and `README.md`. This repo has one top-level folder per component (Data-Pipeline, Model-Development, Retraining-Controller, Agents, Monitoring, Frontend) plus `Deployment/` for infrastructure. The single Airflow on the VM will also run Retraining-Controller and Model-Development DAGs by the Nov 16 live run, so the stack in `docker-compose.yaml` is shared infrastructure, not part of one component.

ADR-013 left one item open: `Data-Pipeline/requirements.txt` asked for `pandas>=2.2` while the Airflow image ships pandas 3.0.5 and pyarrow 25.0.1.

## Decision

- Each component keeps its own top-level folder. `Data-Pipeline/` follows the assignment's example layout.
- One Airflow stack (`docker-compose.yaml`, `.env.example`) stays at the repo root and serves every component. Each component's `dags/` is mounted at `/opt/airflow/dags/<component>`; Data-Pipeline's moves from `/opt/airflow/dags` to `/opt/airflow/dags/data-pipeline`.
- `Data-Pipeline/requirements.txt` pins exact versions from the Airflow constraints for the image (`constraints-3.3.2/constraints-3.12.txt`) and applies that file to transitive dependencies with a `-c` line. pytest is not in the constraints and is pinned to the version the tests passed on. This closes ADR-013's open item.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Compose inside `Data-Pipeline/` | None | Low now; every other component mounts from a sibling folder | The shared stack lives in one component's folder, so the next component's mounts are easy to miss |
| Compose at the root, Data-Pipeline's `dags/` at `/opt/airflow/dags` itself (the layout before this ADR) | None | None now | The next component's mount nests inside Data-Pipeline's, and Docker creates an empty mountpoint folder in `Data-Pipeline/dags/` on the host |
| Compose at the root, each `dags/` at `/opt/airflow/dags/<component>` (chosen) | None | One mount line per component | A component's DAGs don't appear in Airflow until its mount line is added |

## Consequences

- Adding a component's DAGs takes one volume line in the root `docker-compose.yaml`, plus a PYTHONPATH entry once its DAGs import that component's code. Nothing sets PYTHONPATH yet and `Data-Pipeline/scripts/` isn't mounted, so pipeline DAGs can't import the scripts until the DAG work in #22 adds both.
- Task logs from every component land in `Data-Pipeline/logs/`, mounted at `/opt/airflow/logs`.
- DAG files inside the container now sit under `/opt/airflow/dags/data-pipeline/`. Nothing in the repo hardcoded the old path.
- The root README documents the shared stack.
- A new pipeline dependency takes its version from the constraints file; if the file doesn't list it, pin the version the tests passed on.

## Revisit when

- A component needs its own runtime outside the shared Airflow.
- The Airflow image tag in ADR-013 changes; the constraints URL and the pins change with it.
