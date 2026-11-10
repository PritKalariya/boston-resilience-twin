"""The mlflow service in docker-compose.yaml (#104): the client's version, localhost only, small
enough for the VM, writing as the checkout's owner, and needed by nothing but its smoke check."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")  # PyYAML comes with Airflow and with mlflow

ROOT = Path(__file__).resolve().parents[2]
AIRFLOW_SERVICES = ("airflow-apiserver", "airflow-scheduler", "airflow-dag-processor")


@pytest.fixture(scope="module")
def services():
    return yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))["services"]


def flag(service: dict, name: str) -> str | None:
    """The value of --name=value in a service's command."""
    return next((part.split("=", 1)[1] for part in service["command"] if part.startswith(f"--{name}=")), None)


def test_the_server_image_and_the_client_pin_are_the_same_version(services):
    image = re.fullmatch(r"ghcr\.io/mlflow/mlflow:v(\d+\.\d+\.\d+)", services["mlflow"]["image"])
    assert image, services["mlflow"]["image"]
    pins = re.findall(r"^mlflow==(\S+)$", (ROOT / "Data-Pipeline" / "requirements.txt").read_text(encoding="utf-8"), re.M)
    assert pins == [image.group(1)]


def test_every_published_port_binds_localhost_only(services):
    # ADR-016: on the VM a UI is reached through an SSH tunnel, never an open port.
    ports = {name: service.get("ports", []) for name, service in services.items()}
    assert ports["mlflow"] == ["127.0.0.1:${MLFLOW_HOST_PORT:-5000}:5000"]
    assert all(str(port).startswith("127.0.0.1:") for listed in ports.values() for port in listed), ports


def test_nothing_waits_for_the_server_but_its_smoke_check(services):
    waiting = {name for name, service in services.items() if "mlflow" in (service.get("depends_on") or {})}
    assert waiting == {"mlflow-smoke"}
    assert "profiles" not in services["mlflow"] and services["mlflow-smoke"]["profiles"] == ["smoke"]
    assert services["mlflow-smoke"]["environment"]["MLFLOW_TRACKING_URI"] == "http://mlflow:5000"
    for name in AIRFLOW_SERVICES:  # empty unless .env sets it: tasks then log to the local store
        assert services[name]["environment"]["MLFLOW_TRACKING_URI"] == "${MLFLOW_TRACKING_URI:-}", name


def test_the_server_stays_small_and_answers_other_containers(services):
    mlflow = services["mlflow"]
    assert mlflow["mem_limit"]  # #61's budget for the VM
    assert flag(mlflow, "workers") == "1"
    assert mlflow["environment"]["MLFLOW_SERVER_ENABLE_JOB_EXECUTION"] == "false"  # else 7 more processes, 1.6 GiB
    assert {"mlflow", "localhost:*", "127.0.0.1:*"} <= set(flag(mlflow, "allowed-hosts").split(","))
    assert "healthcheck" in mlflow and mlflow["restart"] == "unless-stopped"


def test_the_store_is_in_the_checkouts_folder_and_written_as_its_owner(services):
    mlflow = services["mlflow"]
    assert mlflow["volumes"] == ["./Model-Development/mlflow:/mlflow"]
    assert (ROOT / "Model-Development" / "mlflow" / ".gitkeep").is_file()  # so the checkout owns the folder
    assert flag(mlflow, "backend-store-uri") == "sqlite:////mlflow/mlflow.db"
    assert flag(mlflow, "artifacts-destination") == "/mlflow/artifacts"
    assert mlflow["user"] == "${AIRFLOW_UID:-50000}:0"  # the image would run as root (ADR-029)


def test_env_example_lists_the_mlflow_settings():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    names = {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}
    assert {"MLFLOW_TRACKING_URI", "MLFLOW_HOST_PORT"} <= names
