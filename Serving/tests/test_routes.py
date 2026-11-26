import os
import subprocess
import sys

import pytest

from serving import __main__ as entry
from serving import config
from serving.app import create_app
from serving.config import ConfigError, from_env


def test_web_mode_has_no_predict(make_client, body):
    client = make_client("web")
    assert client.post("/predict", json=body).status_code == 404
    assert client.get("/predict").status_code == 404


def test_model_mode_has_no_page_and_no_forecasts(make_client, model_dir, tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("<h1>map</h1>", encoding="utf-8")
    client = make_client("model", model_dir=model_dir)
    assert client.get("/").status_code == 404
    assert client.get("/forecasts/2026-11-26").status_code == 404
    assert client.get("/health").status_code == 200


def test_all_mode_serves_everything(make_client, model_dir, body, tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("<h1>map</h1>", encoding="utf-8")
    client = make_client("all", model_dir=model_dir)
    assert client.get("/").text == "<h1>map</h1>"
    assert client.post("/predict", json=body).status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/forecasts/2026-11-26").status_code == 404  # no file in the empty forecasts folder


def test_the_page_shadows_no_route(make_client, tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "health").write_text("a file named like a route", encoding="utf-8")
    assert make_client("web").get("/health").json() == {"status": "ok"}


def test_no_index_html_is_a_404_at_the_root(make_client):
    assert make_client("web").get("/").status_code == 404


def test_a_missing_frontend_folder_leaves_no_page(make_client, tmp_path):
    client = make_client("web", frontend_dir=tmp_path / "no-such-folder")
    assert client.get("/").status_code == 404 and client.get("/health").status_code == 200


@pytest.mark.parametrize("value", [None, "", "everything", "MODEL"])
def test_routes_must_be_one_of_the_three(value):
    env = {} if value is None else {"SERVING_ROUTES": value}
    with pytest.raises(ConfigError, match="SERVING_ROUTES"):
        from_env(env)


@pytest.mark.parametrize("value", ["model", "web", "all"])
def test_routes_from_the_environment(value):
    settings = from_env({"SERVING_ROUTES": value, "MODEL_VERSION": "4"})
    assert settings.routes == value and settings.model_version == "4"
    assert settings.model_dir.as_posix() == "/app/model" and settings.forecasts_root == "/app/forecasts"
    assert settings.serves_model == (value != "web") and settings.serves_web == (value != "model")


def test_create_app_refuses_unknown_routes(make_settings):
    with pytest.raises(ConfigError):
        create_app(make_settings("everything"))


@pytest.mark.parametrize("env", [{}, {"SERVING_ROUTES": "bogus"}])
def test_the_process_exits_at_startup_with_one_line(env):
    clean = {k: v for k, v in os.environ.items() if k != "SERVING_ROUTES"}
    result = subprocess.run([sys.executable, "-m", "serving"], env={**clean, **env}, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode != 0
    assert result.stdout == "" and len(result.stderr.strip().splitlines()) == 1
    assert result.stderr.startswith("serving: SERVING_ROUTES")


def test_main_serves_on_the_port_from_the_environment(monkeypatch, make_settings):
    started = {}
    monkeypatch.setattr(entry, "from_env", lambda: make_settings("web"))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(kwargs))
    monkeypatch.setenv("PORT", "9123")
    assert entry.main() == 0
    assert started["port"] == 9123 and started["host"] == "0.0.0.0"
    monkeypatch.delenv("PORT")
    entry.main()
    assert started["port"] == 8080


def test_main_returns_2_on_a_bad_environment(monkeypatch, capsys):
    monkeypatch.delenv("SERVING_ROUTES", raising=False)
    assert entry.main() == 2
    assert "SERVING_ROUTES is not set" in capsys.readouterr().err


def test_settings_are_a_frozen_value():
    settings = from_env({"SERVING_ROUTES": "all"})
    with pytest.raises(AttributeError):
        settings.routes = "model"
    assert config.ROUTES == ("model", "web", "all")
