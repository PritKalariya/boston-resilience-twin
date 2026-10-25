"""Failure alerts for pipeline tasks (#36, the W3-3 alert from #34).

on_task_failure is the on_failure_callback of every daily_pipeline task.
Airflow calls it once a task has failed for good, after any retries, with the
task's context, so a retried network blip sends nothing. It sends one alert
naming the DAG, the task, the run and the error.

Delivery by Slack or email, with the credential read from Secret Manager, comes
with #34 in send_alert. Until a credential is configured, the alert is written
to the task log at ERROR level instead, which is the fallback #34's acceptance
criteria name, and the run is not changed by it. Standard library only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

log = logging.getLogger(__name__)


def alert_message(context: Mapping) -> str:
    """One line: which DAG, task and run failed, on which try, and why."""
    ti = context.get("ti") or context.get("task_instance")
    dag_run = context.get("dag_run")
    dag_id = getattr(ti, "dag_id", None) or getattr(dag_run, "dag_id", None) or "unknown DAG"
    task_id = getattr(ti, "task_id", None) or "unknown task"
    run_id = context.get("run_id") or getattr(dag_run, "run_id", None) or "unknown run"
    try_number = getattr(ti, "try_number", None) or "?"
    error = context.get("exception")
    if error:
        reason = f"{type(error).__name__}: {error}"
    else:
        reason = "no exception recorded (the task may have been killed; check the scheduler log)"
    return f"{dag_id}.{task_id} failed in run {run_id} (try {try_number}): {reason}"


def send_alert(message: str) -> None:
    """Deliver an alert. No channel is configured yet (#34), so it goes to the task log."""
    log.error("ALERT %s", message)


def on_task_failure(context: Mapping) -> None:
    """Airflow on_failure_callback: one alert per failed task. It never raises, so
    a broken alert cannot hide the task's own error."""
    try:
        send_alert(alert_message(context))
    except Exception:
        log.exception("could not send the failure alert")
