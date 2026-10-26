"""The #7 downloads as pipeline steps (pipeline_steps.step_plan)."""

import pytest

import pipeline_steps as steps

WEATHER = ("ingest_noaa_logan", "ingest_nws_forecasts", "ingest_nws_alerts", "ingest_tides")
SLICE = steps.DateRange.from_params({"start": "2025-01-01", "end": "2025-12-31"})


def test_the_four_downloads_are_real_steps_and_retry():
    for step in WEATHER:
        assert step in steps.SCRIPTS and step not in steps.PLANNED
        assert step in steps.NETWORK


@pytest.mark.parametrize("step, args, env", [
    ("ingest_noaa_logan", ["--raw-dir", "weather/noaa_logan"], ("NOAA_CDO_TOKEN",)),
    ("ingest_nws_forecasts", ["forecasts", "--raw-dir", "weather/nws_forecasts"], ("NWS_USER_AGENT",)),
    ("ingest_nws_alerts", ["alerts", "--raw-dir", "weather/nws_alerts"], ("NWS_USER_AGENT",)),
    ("ingest_tides", ["--raw-dir", "tides"], ()),
])
def test_plans_write_to_the_raw_folders_and_name_their_keys(tmp_path, step, args, env):
    plan = steps.step_plan(step, SLICE, tmp_path)
    raw = tmp_path / "Data-Pipeline" / "data" / "raw"
    assert [str(a).replace(f"{raw}/", "") for a in plan.args] == args
    assert plan.env == env
    assert plan.files.data_out == []  # raw history is never cleared before a run


def test_a_missing_key_fails_before_the_script_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    monkeypatch.setattr(steps, "run_script", lambda *a: pytest.fail("the script must not run"))
    with pytest.raises(steps.InputError, match="ingest_noaa_logan cannot start: NOAA_CDO_TOKEN not set"):
        steps.run_step("ingest_noaa_logan", SLICE, tmp_path, run_id="no-key")


def test_a_blank_key_counts_as_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("NWS_USER_AGENT", "   ")
    with pytest.raises(steps.InputError, match="NWS_USER_AGENT not set"):
        steps.check_inputs("ingest_nws_alerts", steps.step_plan("ingest_nws_alerts", SLICE, tmp_path))


def test_with_the_key_set_the_script_runs_with_the_plan_args(tmp_path, monkeypatch):
    monkeypatch.setenv("NWS_USER_AGENT", "(boston-resilience-twin, test@example.org)")
    calls = []
    monkeypatch.setattr(steps, "run_script", lambda module, args: calls.append((module, args)))
    steps.run_step("ingest_nws_alerts", SLICE, tmp_path, run_id="ok")
    assert calls[0][0] == "nws" and calls[0][1][0] == "alerts"
