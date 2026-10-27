"""The daily_pipeline DAG as Airflow parses it. Airflow comes from the Airflow image, not
requirements.txt, so this runs in that image and skips in a plain Python environment.

Airflow is imported in the fixture, not when the module is collected: importing it sets
the root logger to INFO with a stdout handler, which later tests' caplog checks would see.
The fixture puts both back when this module's tests are done."""

import logging
from pathlib import Path

import pytest

import pipeline_steps as steps

DAG_FILE = Path(__file__).resolve().parents[1] / "dags" / "daily_pipeline.py"


@pytest.fixture(scope="module")
def dag():
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)  # before anything imports Airflow
    pytest.importorskip("airflow.sdk")
    try:
        from airflow.dag_processing.dagbag import DagBag
    except ImportError:
        from airflow.models.dagbag import DagBag
    bag = DagBag(dag_folder=str(DAG_FILE))
    assert not bag.import_errors, bag.import_errors
    yield bag.dags["daily_pipeline"]
    root.setLevel(level)
    for handler in root.handlers[:]:
        if handler not in handlers and not type(handler).__module__.startswith("_pytest"):
            root.removeHandler(handler)


def upstream_of(dag, task_id: str) -> set[str]:
    seen, todo = set(), [task_id]
    while todo:
        for task in dag.get_task(todo.pop()).upstream_task_ids:
            if task not in seen:
                seen.add(task)
                todo.append(task)
    return seen


def test_every_real_step_and_placeholder_is_one_task(dag):
    ids = [task.task_id for task in dag.tasks]
    assert len(ids) == len(set(ids))
    # LOCAL: steps done in pipeline_steps itself, such as merge_legacy_311 (#37).
    assert set(ids) == set(steps.SCRIPTS) | set(steps.LOCAL) | set(steps.PLANNED) | {"check_range", "push_data"}


def test_the_feature_table_runs_after_every_download_it_reads(dag):
    before = upstream_of(dag, "feature_table")
    assert {"ingest_noaa_logan", "ingest_tides", "ingest_open_meteo", "ingest_alert_history",
            "ingest_static_layers", "daily_counts"} <= before
    assert "surge_labels" not in before  # labels and features run side by side after the counts


def test_only_the_ingest_tasks_retry(dag):
    for task in dag.tasks:
        assert task.retries == (steps.RETRIES if task.task_id in steps.NETWORK else 0), task.task_id
