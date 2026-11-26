import pytest

import evening_run
from evening_run import Settings, SettingsError, live, settings

HEADER = "setting,value,options,note\n"


def write(tmp_path, body):
    path = tmp_path / "evening_run.csv"
    path.write_text(HEADER + body)
    return path


def test_the_committed_settings():
    config = settings()
    assert config.predict_url == ""  # blank until #161 deploys brt-model
    assert config.timeout_seconds == 120
    assert config.retry_waits == (30, 60, 120, 240)


def test_a_url_loses_its_trailing_slash(tmp_path):
    path = write(tmp_path, "predict_url,https://brt-model-570429679915.us-east1.run.app/,,\n"
                           "predict_timeout_seconds,60,,\npredict_retry_waits_seconds,5;10,,\n")
    assert settings(path) == Settings("https://brt-model-570429679915.us-east1.run.app", 60, (5, 10))


def test_no_waits_means_one_try(tmp_path):
    path = write(tmp_path, "predict_url,,,\npredict_timeout_seconds,60,,\npredict_retry_waits_seconds,,,\n")
    assert settings(path).retry_waits == ()


@pytest.mark.parametrize("body, message", [
    ("predict_url,http://brt-model.run.app,,\npredict_timeout_seconds,60,,\npredict_retry_waits_seconds,5,,\n", "https://"),
    ("predict_url,,,\npredict_timeout_seconds,0,,\npredict_retry_waits_seconds,5,,\n", "1 or more"),
    ("predict_url,,,\npredict_timeout_seconds,soon,,\npredict_retry_waits_seconds,5,,\n", "whole number"),
    ("predict_url,,,\npredict_timeout_seconds,60,,\npredict_retry_waits_seconds,5;-1,,\n", "0 or more"),
    ("predict_url,,,\npredict_timeout_seconds,60,,\n", r"missing settings \['predict_retry_waits_seconds'\]"),
    ("predict_url,,,\npredict_url,,,\npredict_timeout_seconds,60,,\npredict_retry_waits_seconds,5,,\n", "repeats"),
])
def test_bad_settings_are_named(tmp_path, body, message):
    with pytest.raises(SettingsError, match=message):
        settings(write(tmp_path, body))


def test_settings_need_their_columns(tmp_path):
    path = tmp_path / "evening_run.csv"
    path.write_text("name,value\npredict_url,\n")
    with pytest.raises(SettingsError, match="needs the columns setting and value"):
        settings(path)


@pytest.mark.parametrize("value, expected", [("1", True), (" 1 ", True), ("0", False), ("", False), ("true", False)])
def test_live_only_where_the_vm_sets_it(monkeypatch, value, expected):
    monkeypatch.setenv(evening_run.LIVE_ENV, value)
    assert live() is expected


def test_not_live_without_the_variable(monkeypatch):
    monkeypatch.delenv(evening_run.LIVE_ENV, raising=False)
    assert live() is False
