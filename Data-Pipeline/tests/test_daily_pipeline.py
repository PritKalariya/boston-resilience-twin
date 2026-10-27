"""The DAGs in Data-Pipeline/dags as Airflow parses them (DagBag), and daily_pipeline's wiring (#16, #22, #34, #36, #38).

Airflow comes from the Airflow image, not requirements.txt, so these tests run in that image
(`docker compose run --rm --build test`) and skip in a plain Python environment. The test
service sets BRT_TEST_SERVICE=1, which turns the skip into a failure, so CI cannot pass
without running them.

The checks read pipeline_steps' tables (SCRIPTS, LOCAL, PLANNED, NETWORK and each step's
files), not lists of task ids, so a step that #6, #8 or #16 adds is checked without
editing this file.
"""

import logging
import os
import re
from pathlib import Path

import pytest

import alerts
import pipeline_steps as steps

DAGS = Path(__file__).resolve().parents[1] / "dags"
FULL = steps.DateRange()
DEFINES_A_DAG = re.compile(r"^@dag\b|\bDAG\(", re.MULTILINE)  # a file that builds a DAG, not a helper
_BAGS: dict = {}


def dag_bag_class():
    """Airflow's DagBag, imported only when a test needs it (see airflow_logging_is_put_back).
    In the test service a missing Airflow fails the test; elsewhere the test skips."""
    if os.environ.get("BRT_TEST_SERVICE") != "1":
        pytest.importorskip("airflow.sdk", reason="needs Airflow: docker compose run --rm --build test")
    from airflow.dag_processing.dagbag import DagBag

    return DagBag


def dag_bag(folder: Path):
    """A DagBag of folder, built once per folder. safe_mode=False imports every .py file, even one
    without the words "airflow" and "dag" that Airflow's quick scan would skip unseen."""
    key = str(folder)
    if key not in _BAGS:
        _BAGS[key] = dag_bag_class()(dag_folder=key, safe_mode=False)
    return _BAGS[key]


@pytest.fixture(scope="module", autouse=True)
def airflow_logging_is_put_back():
    # Importing Airflow and parsing DAGs set the root logger to INFO and give it a stdout
    # handler. Both are put back after this module, so later tests (caplog) see the logging
    # they expect. pytest's own capture handlers are left alone.
    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    yield
    root.setLevel(level)
    for handler in root.handlers[:]:
        if handler not in before and not type(handler).__module__.startswith("_pytest"):
            root.removeHandler(handler)


@pytest.fixture
def bag():
    # Built inside a test, so conftest.py's network block covers parsing: a DAG file that
    # reached the network at import time would fail here.
    return dag_bag(DAGS)


@pytest.fixture
def dag(bag):
    assert not bag.import_errors, bag.import_errors
    return bag.dags["daily_pipeline"]


def upstream_of(dag, task_id: str) -> set[str]:
    """Every task that runs before task_id, directly or through others."""
    seen, todo = set(), [task_id]
    while todo:
        for task in dag.get_task(todo.pop()).upstream_task_ids:
            if task not in seen:
                seen.add(task)
                todo.append(task)
    return seen


def real_steps(dag) -> list[str]:
    """The daily_pipeline tasks that run a step (pipeline_steps.step_plan knows their files)."""
    return sorted(task.task_id for task in dag.tasks if task.task_id in steps.SCRIPTS or task.task_id in steps.LOCAL)


def plans(dag, step: str) -> list[steps.StepPlan]:
    """A step's plans as the DAG runs it: one per legacy year when the task is mapped over the years (#37)."""
    if dag.get_task(step).is_mapped:
        return [steps.step_plan(step, FULL, year=year) for year in FULL.legacy_years]
    return [steps.step_plan(step, FULL)]


def writes(dag, step: str) -> list[Path]:
    """What a step writes: its data_out and, for a download, the folders it fills. Downloads add to
    their folders and keep history, so they list no data_out (pipeline_steps, #7)."""
    out = []
    for plan in plans(dag, step):
        out += plan.files.data_out
        if step in steps.NETWORK:
            out += [Path(value) for flag, value in zip(plan.args, plan.args[1:]) if flag in ("--raw-dir", "--out-dir")]
    return out


def reads(dag, step: str) -> list[Path]:
    """Every input a step checks before it runs (data_in and config_in)."""
    return [path for plan in plans(dag, step) for path in plan.files.data_in + plan.files.config_in]


def within(path: Path, output: Path) -> bool:
    return path == output or output in path.parents


def test_every_dag_file_imports_without_errors(bag):
    # Syntax and import errors, cycles and a dag_id used twice all land in import_errors.
    assert not bag.import_errors, "\n\n".join(f"{file}:\n{error}" for file, error in bag.import_errors.items())


def test_every_dag_file_defines_a_dag(bag):
    files = {path.name for path in DAGS.glob("*.py") if DEFINES_A_DAG.search(path.read_text(encoding="utf-8"))}
    loaded = {Path(dag.fileloc).name for dag in bag.dags.values()}
    assert files, f"no DAG files found in {DAGS}"
    assert files <= loaded, f"these files build no DAG when Airflow parses them: {sorted(files - loaded)}"


def test_dag_ids_are_unique_across_files():
    seen: dict[str, str] = {}
    for path in sorted(DAGS.glob("*.py")):
        for dag_id in dag_bag(path).dag_ids:
            assert dag_id not in seen, f"dag_id {dag_id!r} is in both {seen[dag_id]} and {path.name}"
            seen[dag_id] = path.name


def test_the_pipeline_and_the_smoke_test_are_there(bag):
    assert {"daily_pipeline", "hello"} <= set(bag.dag_ids)


def test_every_real_step_and_placeholder_is_one_task(dag):
    ids = [task.task_id for task in dag.tasks]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(steps.SCRIPTS) | set(steps.LOCAL) | set(steps.PLANNED) | {"check_range", "push_data"}


def test_every_task_runs_after_the_range_check_and_before_the_push(dag):
    # Anything else at the start would run before the params are checked; anything else at
    # the end would finish after the data is pushed to the DVC remote.
    assert [task.task_id for task in dag.roots] == ["check_range"]
    assert [task.task_id for task in dag.leaves] == ["push_data"]


def test_each_step_runs_after_the_steps_that_write_its_inputs(dag):
    # Read from each step's plan: for example combine_311 after both 311 parsers, the per-year
    # legacy files before merge_legacy_311, and every download before the step that reads it.
    names = real_steps(dag)
    written = {step: writes(dag, step) for step in names}
    missing = []
    for step in names:
        before = upstream_of(dag, step)
        for path in reads(dag, step):
            for writer, outputs in written.items():
                if writer != step and writer not in before and any(within(path, out) for out in outputs):
                    missing.append(f"{step} reads {path.relative_to(steps.REPO)}, which {writer} writes, but can run before it")
    assert not missing, "\n".join(missing)


def test_the_feature_table_runs_after_every_download_it_reads(dag):
    before = upstream_of(dag, "feature_table")
    assert {"ingest_noaa_logan", "ingest_tides", "ingest_open_meteo", "ingest_alert_history",
            "ingest_static_layers", "daily_counts"} <= before
    assert "surge_labels" not in before  # labels and features run side by side after the counts


def test_only_the_ingest_tasks_retry_with_the_shared_backoff(dag):
    for task in dag.tasks:
        if task.task_id in steps.NETWORK:
            assert task.retries == steps.RETRIES, task.task_id
            assert task.retry_delay == steps.RETRY_DELAY, task.task_id
            assert task.retry_exponential_backoff == steps.RETRY_BACKOFF, task.task_id
            assert task.max_retry_delay == steps.MAX_RETRY_DELAY, task.task_id
        else:
            assert task.retries == 0, f"{task.task_id} retries, but only network tasks may (pipeline_steps.NETWORK)"


def test_every_task_sends_the_failure_alert(dag):
    for task in dag.tasks:
        callbacks = task.on_failure_callback
        callbacks = callbacks if isinstance(callbacks, (list, tuple)) else [callbacks]
        assert alerts.on_task_failure in callbacks, task.task_id
