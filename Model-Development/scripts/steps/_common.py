"""Shared plumbing for the model workflow's step adapters (#112, ADR-028).

Each step file is an adapter: PR 3 replaces its body with a call into the owner's
code and keeps the command line (`--artifacts DIR`) and the exit codes. Every step
reads and writes only under the artifacts directory. Exit 0 is ok, 2 is a pipeline
failure; only the gates step (gates.py itself) exits 1, a gate failed.

Standard library only (ADR-013).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

OK = 0
PIPELINE_FAILED = 2

OUTCOMES = ("pass", "fail_gate", "fail_pipeline")

# The step scripts sit in Model-Development/scripts/steps: parents[2] is Model-Development.
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
FIXTURE_ROW = CONFIG_DIR / "reload_fixture_row.json"


class StepError(Exception):
    """A pipeline failure: the step prints the message and exits 2."""


def stub_outcome() -> str:
    value = os.environ.get("STUB_OUTCOME", "pass") or "pass"
    if value not in OUTCOMES:
        raise StepError(f"STUB_OUTCOME {value!r} is not one of {list(OUTCOMES)}")
    return value


def parse_artifacts(description: str, argv: list[str] | None) -> Path:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--artifacts", required=True, help="the artifacts directory")
    return Path(parser.parse_args(argv).artifacts)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StepError(f"cannot read {path}: {exc}") from exc


def read_data_version(artifacts: Path) -> str:
    path = artifacts / "data_version.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StepError(f"cannot read {path}: {exc}") from exc
    if not text:
        raise StepError(f"{path} is empty")
    return text


def load_model(path: Path) -> dict:
    model = read_json(path)
    if not isinstance(model, dict) or model.get("type") != "constant" or "score" not in model:
        raise StepError(f"{path} is not a constant-score model")
    return model


def score(model: dict, row: dict) -> float:
    """Score one feature row. The stub model ignores the row and returns its constant."""
    return float(model["score"])


def run(step, argv: list[str] | None) -> int:
    """Run a step body, turning a StepError or an I/O problem into exit 2."""
    try:
        step(argv)
    except (StepError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return PIPELINE_FAILED
    return OK
