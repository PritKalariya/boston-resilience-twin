import importlib.util
import sys
import types
from pathlib import Path

import pytest

import ingest_311

PIPELINE_DIR = Path(__file__).resolve().parents[1]
DAG_PATH = PIPELINE_DIR / "dags" / "ingest_311_dag.py"


def load_dag_module(name):
    spec = importlib.util.spec_from_file_location(name, DAG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_airflow(monkeypatch):
    """Stub airflow.sdk so the DAG file can be checked without Airflow installed."""
    seen = {"dag": None, "tasks": {}}

    def dag(**kwargs):
        seen["dag"] = kwargs
        return lambda fn: fn

    def task(fn):
        seen["tasks"][fn.__name__] = fn
        return lambda *args, **kwargs: None

    sdk = types.ModuleType("airflow.sdk")
    sdk.dag, sdk.task = dag, task
    airflow = types.ModuleType("airflow")
    airflow.sdk = sdk
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk)
    load_dag_module("ingest_311_dag_stubbed")
    return seen


def test_dag_settings(fake_airflow):
    settings = fake_airflow["dag"]
    assert settings["dag_id"] == "ingest_311"
    assert settings["schedule"] == "@daily"
    assert settings["catchup"] is False
    assert settings["max_active_runs"] == 1
    assert list(fake_airflow["tasks"]) == ["refresh_311_files"]


def test_task_calls_ingestion_with_pipeline_paths(fake_airflow, monkeypatch):
    calls = []
    monkeypatch.setattr(ingest_311, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.delenv("BRT_RAW_311_DIR", raising=False)
    fake_airflow["tasks"]["refresh_311_files"]()
    argv = calls[0]
    assert argv[argv.index("--raw-dir") + 1] == str(PIPELINE_DIR / "data" / "raw" / "311")
    sources = Path(argv[argv.index("--sources") + 1])
    assert sources == PIPELINE_DIR / "config" / "ingest_311_sources.csv" and sources.exists()


def test_task_uses_raw_dir_from_environment(fake_airflow, monkeypatch):
    calls = []
    monkeypatch.setattr(ingest_311, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setenv("BRT_RAW_311_DIR", "/opt/airflow/data/raw/311")
    fake_airflow["tasks"]["refresh_311_files"]()
    assert calls[0][calls[0].index("--raw-dir") + 1] == "/opt/airflow/data/raw/311"


def test_task_fails_when_ingestion_fails(fake_airflow, monkeypatch):
    monkeypatch.setattr(ingest_311, "main", lambda argv: 1)
    with pytest.raises(RuntimeError, match="311 ingestion failed"):
        fake_airflow["tasks"]["refresh_311_files"]()


def test_dag_loads_in_real_airflow():
    pytest.importorskip("airflow")
    dag = load_dag_module("ingest_311_dag_real").dag_ingest_311
    assert dag.dag_id == "ingest_311"
    assert list(dag.task_ids) == ["refresh_311_files"]
