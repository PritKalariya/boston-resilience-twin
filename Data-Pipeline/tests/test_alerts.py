import logging
from types import SimpleNamespace

import alerts


def failed_context(error=ValueError("population.csv is empty")):
    ti = SimpleNamespace(dag_id="daily_pipeline", task_id="ingest_static_layers", try_number=3)
    return {"ti": ti, "run_id": "slice-2025", "exception": error}


def test_alert_names_the_dag_task_run_try_and_error():
    assert alerts.alert_message(failed_context()) == (
        "daily_pipeline.ingest_static_layers failed in run slice-2025 (try 3): ValueError: population.csv is empty"
    )


def test_alert_falls_back_to_the_dag_run():
    context = {"dag_run": SimpleNamespace(dag_id="daily_pipeline", run_id="full-build"), "exception": RuntimeError("boom")}
    assert alerts.alert_message(context) == "daily_pipeline.unknown task failed in run full-build (try ?): RuntimeError: boom"


def test_alert_with_an_empty_context():
    assert alerts.alert_message({}) == (
        "unknown DAG.unknown task failed in run unknown run (try ?): "
        "no exception recorded (the task may have been killed; check the scheduler log)"
    )


def test_failure_callback_writes_the_alert_to_the_task_log(caplog):
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.on_task_failure(failed_context())
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.messages[-1] == (
        "ALERT daily_pipeline.ingest_static_layers failed in run slice-2025 (try 3): ValueError: population.csv is empty"
    )


def test_failure_callback_never_raises(monkeypatch, caplog):
    def broken(message):
        raise ConnectionError("Slack is down")

    monkeypatch.setattr(alerts, "send_alert", broken)
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.on_task_failure(failed_context())
    assert caplog.messages[-1] == "could not send the failure alert"
