# ADR-023: Pipeline code, config and data mount under /opt/airflow/repo at host paths

- Status: accepted
- Date: 2026-10-27
- Owner: PritKalariya
- Phase: data

## Context

ADR-017 put the shared Airflow stack's `docker-compose.yaml` at the repo root and set where each component's `dags/` mounts, but left open how a DAG reaches code outside `dags/`: "Code outside `dags/` needs its own mount and a PYTHONPATH entry. Neither exists yet for `Data-Pipeline/scripts/`."

#22 (PR #72) added that mount and PYTHONPATH entry, and #6 (PR #73) was the first DAG work to depend on it. #6's first version mounted `Data-Pipeline/scripts`, `config` and `data` on their own, at `/opt/airflow/scripts`, `/opt/airflow/config` and `/opt/airflow/data`, and its `ingest_311_dag.py` found the scripts folder by computing `Path(__file__).resolve().parents[1] / "scripts"`. That computation assumed the DAG file sat directly under a folder laid out like the host repo. Once ADR-017 moved Data-Pipeline's DAGs to `/opt/airflow/dags/data_pipeline`, the assumption no longer held, and review on #73 flagged it: the `__file__`-derived path pointed at the wrong place. #72 had meanwhile landed its own mount of `Data-Pipeline/scripts` under `/opt/airflow/repo/`, so #6 also risked running two different mount schemes for the same folder in one compose file.

#73 adopted #72's layout instead. This ADR records what both merged PRs put in `docker-compose.yaml`.

## Decision

- `Data-Pipeline/scripts`, `Data-Pipeline/config`, `docs/data` and `Data-Pipeline/data` mount under `/opt/airflow/repo/` at the same relative paths they have on the host: `/opt/airflow/repo/Data-Pipeline/scripts`, `/opt/airflow/repo/Data-Pipeline/config`, `/opt/airflow/repo/docs/data`, `/opt/airflow/repo/Data-Pipeline/data`. `scripts`, `config` and `docs/data` are read-only (`:ro`); `data` is writable, since tasks read and write under it.
- `PYTHONPATH=/opt/airflow/repo/Data-Pipeline/scripts` is set on `x-airflow-common`'s environment, so every Airflow service (`airflow-init`, `airflow-apiserver`, `airflow-scheduler`, `airflow-dag-processor`) has it, not just the scheduler that runs tasks.
- `airflow-init`'s chown step covers both `/opt/airflow/logs` and `/opt/airflow/repo/Data-Pipeline/data`, so a task running as `AIRFLOW_UID` can write under `data` where the checkout is root-owned (the VM).
- DAGs stay at `/opt/airflow/dags/data_pipeline` per ADR-017. This ADR only covers code and data outside `dags/`.
- A DAG imports a script as `from <module> import ...` (bare module name), relying on PYTHONPATH, not on `data_pipeline.<module>`, which is reserved for helpers inside the DAG's own `dags/` folder per ADR-017.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Separate `/opt/airflow/{scripts,config,data}` mounts, found by `Path(__file__)` math in each DAG (#6's first version) | None | One mount line per folder, plus path math to maintain in every DAG file | The math assumes the DAG file's position matches the host layout; ADR-017's `dags/data_pipeline` nesting broke it, per Srikar's review on #73. Also put a second, different mount scheme for `Data-Pipeline/scripts` next to #72's in the same compose file |
| `/opt/airflow/repo/` mounts at host-relative paths, fixed PYTHONPATH (chosen) | None | One mount line per folder, one PYTHONPATH entry, no per-DAG path math | A script importing something added to a later component's scripts folder needs that component's PYTHONPATH entry too (see Consequences) |
| Install `Data-Pipeline/scripts` as a package in the Airflow image (editable install) | None (image already rebuilds for `requirements.txt`, ADR-022) | Needs packaging setup (`pyproject.toml`, build backend) not yet in the repo | Worth revisiting; not attempted here |
| `sys.path.insert` in each DAG file, as `ingest_311_dag.py` first did | None | Repeated per DAG file, easy to miss on a new one | Same shadowing risk as PYTHONPATH, spread across files instead of one compose line, so it drifts silently when a DAG is added without it |

## Consequences

- `/opt/airflow/repo/Data-Pipeline/scripts` sits ahead of the standard library and site-packages on `sys.path` in every Airflow process. A script named like a stdlib module or an installed dependency shadows it. `test_scripts_do_not_shadow_installed_packages` (`Data-Pipeline/tests/test_pipeline_steps.py`) guards against this.
- A second component that adds its own scripts needs its own PYTHONPATH entry (or a different arrangement), and script module names must stay unique across components, since they all land on the same `sys.path`.
- Code outside `dags/` is read-only in the container (`scripts`, `config`, `docs/data`); only `Data-Pipeline/data` is writable. A script that tries to write under `scripts` or `config` fails in the container even if it works on the host.
- `.git`, `.dvc` and `.dvcignore` are mounted separately for the `push_data` task's DVC calls; this ADR covers only the pipeline code, config and data mounts.

## Revisit when

- A second component (Model-Development, Retraining-Controller, Agents, Monitoring, Frontend) adds its own scripts outside `dags/` and needs a mount and PYTHONPATH entry of its own.
- A script name collides with another component's script or an installed package, and `test_scripts_do_not_shadow_installed_packages` or a real import catches it.
