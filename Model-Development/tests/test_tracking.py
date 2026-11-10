"""MLflow tracking (#104): the server MLFLOW_TRACKING_URI names, else a local store, and the smoke check.

No test needs a server or the network: the logging tests write to a local store in tmp_path,
with the mlflow client from Data-Pipeline/requirements.txt (they skip where it is missing).
"""

from __future__ import annotations

import http.server
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mlflow_smoke  # noqa: E402
import tracking  # noqa: E402

CLOSED = "http://127.0.0.1:9"  # nothing listens on the discard port
LOGGED = {"param": "1", "metric": 0.5, "artifacts": ["smoke.txt"]}


@pytest.fixture
def no_server(monkeypatch):
    """MLFLOW_TRACKING_URI unset; MLflow's own tracking URI is reset afterwards."""
    monkeypatch.delenv(tracking.ENV, raising=False)
    yield
    if "mlflow" in sys.modules:
        sys.modules["mlflow"].set_tracking_uri(None)


@pytest.fixture
def fake_server():
    """A local HTTP server whose /health answers answer["body"]; yields (its URL, answer)."""
    answer = {"body": b"OK"}

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(answer["body"])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", answer
    finally:
        server.shutdown()
        server.server_close()


# Where runs go


def test_the_server_in_mlflow_tracking_uri_is_used(monkeypatch):
    monkeypatch.setenv(tracking.ENV, " http://mlflow:5000 ")
    assert tracking.tracking_uri() == "http://mlflow:5000"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_empty_means_the_local_store(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv(tracking.ENV, raising=False)
    else:
        monkeypatch.setenv(tracking.ENV, value)  # the compose default passes an empty value
    assert tracking.tracking_uri(tmp_path) == "sqlite:///" + (tmp_path / "mlflow.db").resolve().as_posix()
    default = SCRIPTS.parent / "mlruns" / "mlflow.db"  # Model-Development/mlruns
    assert tracking.tracking_uri() == "sqlite:///" + default.resolve().as_posix()


# The server check


def test_a_server_must_answer_health_with_ok(fake_server):
    url, answer = fake_server
    tracking.check_server(url)
    tracking.check_server(url + "/")
    answer["body"] = b"Not MLflow"
    with pytest.raises(ConnectionError, match="not OK"):
        tracking.check_server(url)


def test_a_server_that_does_not_answer_fails_at_once_and_says_how_to_log_locally(monkeypatch):
    monkeypatch.setenv(tracking.ENV, CLOSED)
    with pytest.raises(ConnectionError, match=f"MLFLOW_TRACKING_URI is {CLOSED}.*unset MLFLOW_TRACKING_URI"):
        tracking.setup("smoke-test")


# Logging to a local store


def test_the_smoke_run_reads_back_and_nothing_lands_in_the_current_folder(no_server, tmp_path, monkeypatch, capsys):
    pytest.importorskip("mlflow")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    store = tmp_path / "store"
    assert mlflow_smoke.main(["--local-dir", str(store)]) == 0
    out = capsys.readouterr().out
    assert "mlflow smoke OK" in out and f"mlflow ui --backend-store-uri {tracking.local_uri(store)}" in out
    assert (store / "mlflow.db").is_file()
    assert [p.name for p in (store / "artifacts" / "smoke-test").rglob("*.txt")] == ["smoke.txt"]
    assert list(elsewhere.iterdir()) == []  # MLflow's defaults would write mlflow.db and mlruns/ here


def test_a_second_run_goes_to_the_same_experiment(no_server, tmp_path):
    pytest.importorskip("mlflow")
    first, second = mlflow_smoke.smoke(tmp_path), mlflow_smoke.smoke(tmp_path)
    assert first["experiment_id"] == second["experiment_id"] and first["run_id"] != second["run_id"]
    assert mlflow_smoke.problems(first) == mlflow_smoke.problems(second) == []


def test_without_a_folder_the_store_is_model_development_mlruns(no_server, tmp_path, monkeypatch):
    pytest.importorskip("mlflow")
    monkeypatch.setattr(tracking, "LOCAL_DIR", tmp_path / "mlruns")  # stands in for Model-Development/mlruns
    assert mlflow_smoke.main([]) == 0
    assert (tmp_path / "mlruns" / "mlflow.db").is_file()


# The smoke check's verdict


def test_problems_names_each_part_that_did_not_read_back():
    assert mlflow_smoke.problems(LOGGED) == []
    found = mlflow_smoke.problems({"param": None, "metric": 0.25, "artifacts": []})
    assert len(found) == 3 and "smoke_param" in found[0] and "smoke_metric" in found[1] and "smoke.txt" in found[2]


def test_the_link_is_the_run_page_as_the_hosts_browser_sees_it(monkeypatch):
    result = {"uri": "http://mlflow:5000", "experiment_id": "1", "run_id": "abc"}
    monkeypatch.delenv(mlflow_smoke.UI_ENV, raising=False)
    assert mlflow_smoke.where_to_look(result) == "http://mlflow:5000/#/experiments/1/runs/abc"
    monkeypatch.setenv(mlflow_smoke.UI_ENV, "http://127.0.0.1:5050/")
    assert mlflow_smoke.where_to_look(result) == "http://127.0.0.1:5050/#/experiments/1/runs/abc"


def test_main_fails_when_the_server_does_not_answer(monkeypatch, capsys):
    monkeypatch.setenv(tracking.ENV, CLOSED)
    assert mlflow_smoke.main([]) == 1
    assert "did not answer" in capsys.readouterr().err


def test_main_fails_when_the_run_does_not_read_back(monkeypatch, capsys):
    lost = {**LOGGED, "uri": "http://mlflow:5000", "experiment_id": "1", "run_id": "r", "artifacts": []}
    monkeypatch.setattr(mlflow_smoke, "smoke", lambda local_dir=None: lost)
    assert mlflow_smoke.main([]) == 1
    assert "artifact smoke.txt is not among []" in capsys.readouterr().err
