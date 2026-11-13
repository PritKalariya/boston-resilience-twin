"""INTERFACE FOR #111 (Kirtan): replace this body; keep the command line and exit codes.
Called as: python Model-Development/scripts/steps/step_registry_push.py --artifacts DIR
Reads: `evaluation.json` (`winner`, `winner_run`), `runs_dir/<winner_run>/model.joblib` and `run.json`
  (`mlflow.run_id`, `settings.splits`), `data_version.txt`, `gate_report.json` (`decision`, `gates_sha256`,
  `fairness_group_sha256`), `image_digest.txt`, and `GITHUB_SHA` from the environment.
Writes: `registry.json`: `model_version`, `pushed` (false), `model` (name, `path` inside the artifacts
  folder, sha256), `data_version`, `git_commit`, `mlflow_run_id`, `image_digest`, `gates_sha256`,
  `fairness_group_sha256`, `windows` (train, validation and test, each first and last day) and `decision`.
  Those are the version stamps the registry stores with the model (Sculley 2015, Xu 2024).
Exit: 0 ok, 2 pipeline failed
Must hold: runs only after the gates and the reload check passed, and only for the decisions `promote`
  and `keep_champion`; never changes the champion alias (step_set_champion.py does); the version stamps
  above are stored with the model.

Step: register the model (#112, ADR-028, ADR-032). STUB. Main only. No network.

A missing `image_digest.txt` or `GITHUB_SHA` is a warning, and the field is null.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import step_common as C


def read_image_digest(artifacts: Path) -> str | None:
    path = artifacts / "image_digest.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        text = ""
    if not text:
        print(f"::warning::{path} is missing or empty: registry.json gets no image digest")
        return None
    return text


DESCRIPTION = "Step: register the model"


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(DESCRIPTION, argv, with_config=True)
    settings = C.load_settings(config)
    evaluation = C.read_object(artifacts / "evaluation.json")
    winner, folder = evaluation.get("winner"), settings.runs_dir / str(evaluation.get("winner_run"))
    run = C.read_object(folder / "run.json")
    model_file = folder / "model.joblib"
    try:
        model_sha = hashlib.sha256(model_file.read_bytes()).hexdigest()
    except OSError as exc:
        raise C.StepError(f"cannot read {model_file}: {exc}") from exc
    data_version = C.read_data_version(artifacts)
    report = C.read_object(artifacts / "gate_report.json")
    if report.get("decision") not in ("promote", "keep_champion"):
        raise C.StepError(f"gate decision {report.get('decision')!r} cannot be registered")
    splits = run.get("settings", {}).get("splits")
    mlflow_run = run.get("mlflow", {}).get("run_id")
    if not isinstance(splits, dict) or set(splits) != {"train", "validation", "test"} or not mlflow_run:
        raise C.StepError(f"{folder / 'run.json'} has no windows (settings.splits) or no MLflow run id")
    commit = os.environ.get("GITHUB_SHA", "").strip() or None
    if commit is None:
        print("::warning::GITHUB_SHA is not set: registry.json gets no git commit")
    C.write_json(artifacts / "registry.json", {
        "model_version": f"stub-{winner}-{data_version.removeprefix('sha256:')[:12]}",
        "pushed": False,
        "model": {"name": winner, "path": f"runs/{folder.name}/model.joblib", "sha256": model_sha},
        "data_version": data_version,
        "git_commit": commit,
        "mlflow_run_id": mlflow_run,
        "image_digest": read_image_digest(artifacts),
        "gates_sha256": report.get("gates_sha256"),
        "fairness_group_sha256": report.get("fairness_group_sha256"),
        "windows": {name: {"first": window[0], "last": window[1]} for name, window in splits.items()},
        "decision": report.get("decision"),
    })


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
