"""Hello DAG: smoke test for the local Airflow stack.

Trigger it by hand. The task log records the Python and Airflow versions,
and the run fails if the image's Python is not the minor version that
ADR-013 pins.
"""

import logging
import platform
from importlib.metadata import version

import pendulum
from airflow.sdk import dag, task

PINNED_PYTHON = "3.12"  # ADR-013

log = logging.getLogger("airflow.task")


@dag(
    dag_id="hello",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    tags=["setup"],
    doc_md=__doc__,
)
def hello():
    @task
    def report_versions() -> dict[str, str]:
        versions = {
            "python": platform.python_version(),
            "airflow": version("apache-airflow"),
            "task_sdk": version("apache-airflow-task-sdk"),
        }
        log.info("Python %s", versions["python"])
        log.info("Airflow %s, task SDK %s", versions["airflow"], versions["task_sdk"])
        return versions

    @task
    def check_python_pin(versions: dict[str, str]) -> None:
        minor = ".".join(versions["python"].split(".")[:2])
        if minor != PINNED_PYTHON:
            raise RuntimeError(
                f"ADR-013 pins Python {PINNED_PYTHON}, but the image runs {versions['python']}"
            )
        log.info("Python %s matches the ADR-013 pin", versions["python"])

    check_python_pin(report_versions())


hello()
