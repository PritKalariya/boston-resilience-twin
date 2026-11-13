"""Step: post one Slack message about the run (#112, ADR-028).

Runs on the runner, not in the image: it needs the webhook, and nothing else does.
The webhook is read from the SLACK_ALERT_WEBHOOK environment variable, the name the
Airflow pipeline uses (Data-Pipeline/scripts/alerts.py). Routing:

  gates exited 1, nothing else failed -> "gate failed" (PR and main)
  any other failure, including a later step that failed after a PR's gates exited 1
                                 -> "pipeline failed" (PR and main)
  success on main                -> "training complete" with the decision
  success on a PR, or cancelled  -> no message

A "gate failed" or "pipeline failed" message names the failed step and says "not pushed":
the model was not registered and the champion is unchanged (ADR-032). The workflow passes the
step's name in --failed-step. The message carries validation numbers only, never a test
metric: the gate report holds none (Xu 2024).

With the variable empty (also a fork PR, which gets no secrets) it prints a
::warning:: and the message it would have sent, and exits 0. A Slack outage is a
warning too, never a failed run. The webhook value is never printed. Standard
library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEBHOOK_ENV = "SLACK_ALERT_WEBHOOK"
TIMEOUT_SECONDS = 10

GATE_FAILED = "gate failed"
PIPELINE_FAILED = "pipeline failed"
TRAINING_COMPLETE = "training complete"


def route(job_status: str, gates_exit: str, is_main: bool) -> str | None:
    """The message kind for a run, or None when nobody needs one.

    A failed job is "pipeline failed" unless the gates step is the only failure. Gates exit 1
    is the only failure when the job passed (a PR: the gates step succeeds with a warning), or
    on main, where the gates step fails the job and no later step runs. On a PR, gates exit 1
    followed by a failed later step (the reload check, say) is a pipeline failure.
    """
    gates_failed = gates_exit.strip() == "1"
    if gates_failed and (is_main or job_status == "success"):
        return GATE_FAILED
    if job_status == "cancelled":
        return None
    if job_status != "success":
        return PIPELINE_FAILED
    return TRAINING_COMPLETE if is_main else None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value: object) -> str:
    return f"{value:.4f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "n/a"


def _escape(text: str) -> str:
    """Slack's control characters, & first (the same rule as Data-Pipeline/scripts/alerts.py)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


NOT_PUSHED = "Model not pushed: the registry and the champion are unchanged"


def build_message(kind: str, artifacts: Path, repo: str, ref: str, run_url: str, failed_step: str = "") -> str:
    try:
        data_version = (artifacts / "data_version.txt").read_text(encoding="utf-8").strip() or "n/a"
    except (OSError, ValueError):  # missing folder or file, or bytes that are not text
        data_version = "n/a"
    report = _read_json(artifacts / "gate_report.json")
    f1_gate = report.get("f1_gate") if isinstance(report.get("f1_gate"), dict) else {}
    fairness_gate = report.get("fairness_gate") if isinstance(report.get("fairness_gate"), dict) else {}
    decision = report.get("decision") or "n/a"
    lines = [
        f"Model pipeline: {kind}",
        f"Repo: {repo}",
        f"Ref: {ref}",
        f"Run: {run_url}",
        f"Data version: {data_version}",
        f"Decision: {decision}",
        f"F1 margin: {_number(f1_gate.get('margin'))}",
        f"Worst-group recall gap: {_number(fairness_gate.get('gap'))}",
    ]
    if kind in (GATE_FAILED, PIPELINE_FAILED):
        lines.insert(1, f"Failed step: {failed_step.strip() or 'unknown'}")
        lines.insert(2, NOT_PUSHED)
    return _escape("\n".join(lines))


def post(url: str, payload: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post the model pipeline result to Slack (#112).")
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--job-status", required=True, choices=["success", "failure", "cancelled"])
    parser.add_argument("--gates-exit", default="", help="the gates step's exit code, empty if it did not run")
    parser.add_argument("--failed-step", default="", help="the name of the step that failed, empty if none did")
    parser.add_argument("--main", action="store_true", help="the run is on main")
    args = parser.parse_args(argv)

    kind = route(args.job_status, args.gates_exit, args.main)
    if kind is None:
        print(f"no Slack message for status {args.job_status} on {'main' if args.main else 'a PR'}")
        return 0
    message = build_message(kind, Path(args.artifacts), args.repo, args.ref, args.run_url, args.failed_step)
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if not webhook:
        print(f"::warning::{WEBHOOK_ENV} is empty (not set, or a fork PR): no Slack message sent")
        print(message)
        return 0
    if not webhook.startswith("https://"):
        print(f"::warning::{WEBHOOK_ENV} is not an https URL: no Slack message sent")
        return 0
    try:
        post(webhook, {"text": message})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The exception text can carry the URL: print only its type.
        print(f"::warning::Slack post failed ({type(exc).__name__}); the run result is unchanged")
        return 0
    print(f"Slack message sent: {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
