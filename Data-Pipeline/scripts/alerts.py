"""Failure alerts for pipeline tasks (#36, #34).

on_task_failure is the on_failure_callback of every daily_pipeline task.
Airflow calls it once a task has failed for good, after any retries, with the
task's context, so a retried network blip sends nothing. It sends one alert
naming the DAG, the task, the run and the error; for a failed validation the
error is ValidationFailed, which names each failing check (#17).

send_alert always writes the alert to the task log at ERROR level. When
SLACK_ALERT_WEBHOOK is set (from the gitignored .env, like #4's API keys), it
also posts the alert to that Slack incoming webhook. Without it, the task log
is the only channel, which is the fallback #34's acceptance criteria name, and
the run is not changed by it. On airflow-vm the webhook comes from Secret
Manager with #64. The webhook URL is the credential, so it is never logged.
Standard library only.

    python Data-Pipeline/scripts/alerts.py --test   # post one test alert
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Mapping

SLACK_WEBHOOK_ENV = "SLACK_ALERT_WEBHOOK"
# Incoming webhooks live here; anything else is a typo, and sending pipeline
# errors to an unknown address is worse than not sending them.
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/"
SLACK_TIMEOUT_SECONDS = 10
# Slack shows up to 40,000 characters; a long ValidationFailed list is cut well before that.
SLACK_MAX_CHARS = 3000

log = logging.getLogger(__name__)


class AlertNotSent(RuntimeError):
    """Slack did not take the alert. The message never includes the webhook URL."""


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


def slack_webhook() -> str | None:
    """The configured webhook URL, or None when alerts go to the task log only."""
    url = os.environ.get(SLACK_WEBHOOK_ENV, "").strip()
    if not url:
        return None
    if not url.startswith(SLACK_WEBHOOK_PREFIX):
        log.warning("%s is set but is not a Slack incoming webhook (%s...); the alert stays in the task log",
                    SLACK_WEBHOOK_ENV, SLACK_WEBHOOK_PREFIX)
        return None
    return url


def slack_text(message: str) -> str:
    text = f":rotating_light: *Pipeline alert*\n{message}"
    if len(text) > SLACK_MAX_CHARS:
        cut = "\n... (cut; the full alert is in the task log)"
        text = text[: SLACK_MAX_CHARS - len(cut)] + cut
    return text


def post_to_slack(url: str, message: str, timeout: float = SLACK_TIMEOUT_SECONDS) -> None:
    """POST one message to a Slack incoming webhook, which answers 200 "ok"."""
    body = json.dumps({"text": slack_text(message)}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        # Slack answers 403/404 for a revoked or mistyped webhook; e.reason is its short text.
        raise AlertNotSent(f"Slack refused the alert: HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AlertNotSent(f"Slack could not be reached: {getattr(e, 'reason', e)}") from None
    if status != 200:
        raise AlertNotSent(f"Slack answered HTTP {status}")


def send_alert(message: str, post=None) -> None:
    """Write the alert to the task log, and post it to Slack when a webhook is set."""
    log.error("ALERT %s", message)
    url = slack_webhook()
    if url is None:
        return
    (post or post_to_slack)(url, message)
    log.info("alert sent to Slack")


def on_task_failure(context: Mapping) -> None:
    """Airflow on_failure_callback: one alert per failed task. It never raises, so
    a broken alert cannot hide the task's own error."""
    try:
        send_alert(alert_message(context))
    except Exception as e:
        log.error("could not send the failure alert: %s", e)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send a test alert through the configured channel.")
    parser.add_argument("--test", action="store_true", help="post one test alert")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.test:
        parser.error("nothing to do; pass --test")
    if slack_webhook() is None:
        print(f"{SLACK_WEBHOOK_ENV} is not set (or not a Slack webhook): the alert goes to the log only")
    send_alert("test alert from alerts.py --test: if you can read this in Slack, pipeline alerts work")


if __name__ == "__main__":
    main()
