"""Shared plumbing for the model workflow's step adapters (#112, ADR-028, ADR-032).

Each step file runs one stage of the workflow in the training image. The contract is one
command line, `--artifacts DIR`, and these exit codes: 0 ok, 2 pipeline failed; only the
gates step (gates.py itself) exits 1, a gate failed. A step reads and writes the artifacts
directory; the training steps also write the run folders, which the workflow mounts from
`artifacts/runs` (`runs_dir` of training.csv), `artifacts/mlruns` and `artifacts/reports`.

The steps that train or read a run take one more, optional flag, `--config`, the
training.csv to load (default: the committed one). The workflow never passes it; tests do,
to point a step at a small temporary repo.

Standard library only here (ADR-013); the step files import the training modules they need.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OK = 0
PIPELINE_FAILED = 2

# The step scripts sit in Model-Development/scripts/steps: parents[1] is Model-Development/scripts.
SCRIPTS_DIR = Path(__file__).resolve().parents[1]

# The run folders of one workflow run: runs_dir/<run id>. Fixed, so a later step finds them.
RUN_PREFIX = "ci-"

# Run as `python .../steps/step_x.py`, sys.path starts with the steps folder only. Put the
# scripts folder after it, so a step can `import train` or `import evaluate` and get #106's
# modules (ADR-027). Every file in steps/ starts with `step_` so no name collides with them;
# appended, never first, so nothing in scripts/ can shadow a step module either.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))


class StepError(Exception):
    """A pipeline failure: the step prints the message and exits 2."""


def run_id(model: str) -> str:
    """The fixed run folder name of a model's training run in the workflow."""
    return f"{RUN_PREFIX}{model}"


def parse_artifacts(description: str, argv: list[str] | None, with_config: bool = False) -> tuple[Path, Path | None]:
    """The artifacts directory and, when with_config, the training.csv to load (None: the default)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--artifacts", required=True, help="the artifacts directory")
    if with_config:
        parser.add_argument("--config", help="training.csv to load (default: the committed one)")
    args = parser.parse_args(argv)
    return Path(args.artifacts), (Path(args.config) if with_config and args.config else None)


def load_settings(config: Path | None):
    """training.csv as training_data.load_settings reads it; a bad file is a pipeline failure."""
    import training_data  # here, so the notify step and this module import with the standard library only

    try:
        return training_data.load_settings(config or training_data.DEFAULT_CONFIG)
    except (OSError, ValueError) as exc:
        raise StepError(f"cannot load the training settings: {exc}") from exc


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StepError(f"cannot read {path}: {exc}") from exc


def read_object(path: Path) -> dict:
    """A JSON file that must hold an object."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise StepError(f"{path} is not a JSON object")
    return data


def read_data_version(artifacts: Path) -> str:
    path = artifacts / "data_version.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StepError(f"cannot read {path}: {exc}") from exc
    if not text:
        raise StepError(f"{path} is empty")
    return text


def main_run() -> bool:
    """A run on main (model.yml MAIN_RUN, passed in by in_image.sh): the registry steps need project 2
    access there, so a refusal fails the step instead of skipping it (#111)."""
    import os

    return os.environ.get("MAIN_RUN", "").strip() == "true"


def registry_main(args: list[str], what: str) -> None:
    """Run registry.py's command line in this process; a non-zero exit is a pipeline failure."""
    import registry

    flags = ["--require-access"] if main_run() else []
    code = registry.main([*args, *flags])
    if code != 0:
        raise StepError(f"registry.py {what} exited {code}")


def run(step, argv: list[str] | None) -> int:
    """Run a step body; any exception is a pipeline failure, exit 2 (exit 1 is the gates').

    argparse exits through SystemExit, which is not an Exception and passes through.
    """
    try:
        step(argv)
    except StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return PIPELINE_FAILED
    except Exception as exc:  # a bug or a damaged input must not escape as a traceback with exit 1
        print(f"error: pipeline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return PIPELINE_FAILED
    return OK
