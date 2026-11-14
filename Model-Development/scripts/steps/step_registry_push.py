"""Step: register the model (#111, #112, ADR-006, ADR-028, ADR-032). Main only.

Called as: python Model-Development/scripts/steps/step_registry_push.py --artifacts DIR
Reads: `evaluation.json` (`winner`, `winner_run`), `runs_dir/<winner_run>/` (model.joblib, run.json,
  metrics.json, validation_counts.json), `data_version.txt`, `gate_report.json` (`decision`, `data_version`,
  `gates_sha256`, `fairness_group_sha256`), `reload_check.json`, `image_digest.txt`, and `GITHUB_SHA`
  from the environment.
Writes: `push.json`, the push record `registry.py promote` reads (or `{"skipped": ...}`), and
  `registry.json`: `model_version` (`<model resource>@<version id>`, null when skipped), `pushed`, `model`
  (name, `path` inside the artifacts folder, sha256), `data_version`, `git_commit`, `mlflow_run_id`,
  `image_digest`, `gates_sha256`, `fairness_group_sha256`, `windows` (train, validation and test, each first
  and last day), `decision` and `files_uri`. Those are the version stamps the registry stores with the model
  (Sculley 2015, Xu 2024).
Exit: 0 ok, 2 pipeline failed
Must hold: runs only after the gates and the reload check passed, and only for the decisions `promote`
  and `keep_champion`; never changes the champion alias (step_set_champion.py does); the version stamps
  above are stored with the model.

This is `registry.py push --evaluation` (#111): the files and `metadata.json` go to the model bucket, the
version gets the `candidate` alias and the MLflow run is tagged. Before it, this step checks that the reload
check passed for the same winner and that the gate report is for the pulled data version. Credentials come
from the file in_image.sh mounts (ADR-035). On main (`MAIN_RUN=true`) no project 2 access, a missing
`GITHUB_SHA` or a missing registry digest of the training image fails the step; elsewhere the push is
skipped with a log line, and a missing digest or commit is a warning.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import step_common as C

DESCRIPTION = "Step: register the model"
DECISIONS = ("promote", "keep_champion")


def read_image_digest(artifacts: Path, serving_image: str) -> tuple[str | None, str | None]:
    """(the text of image_digest.txt, its sha256:<hex> when it is a registry digest of the serving image).

    On a pull request the file holds the local image ID, which is no registry digest: a Vertex version
    must name an image Vertex can pull."""
    path = artifacts / "image_digest.txt"
    try:
        text = path.read_text(encoding="utf-8").strip() or None
    except (OSError, ValueError):
        text = None
    repository, at, digest = (text or "").partition("@")
    if at and repository == serving_image:
        return text, digest
    message = f"{path} holds no registry digest of {serving_image}: {text!r}"
    if C.main_run():
        raise C.StepError(message)
    print(f"::warning::{message}; the version names the image without a digest")
    return text, None


def checked_inputs(artifacts: Path, settings) -> tuple[dict, dict, str]:
    """(evaluation.json, gate_report.json, the data version), after the checks this step must hold."""
    evaluation = C.read_object(artifacts / "evaluation.json")
    data_version = C.read_data_version(artifacts)
    report = C.read_object(artifacts / "gate_report.json")
    if report.get("decision") not in DECISIONS:
        raise C.StepError(f"gate decision {report.get('decision')!r} cannot be registered")
    if report.get("data_version") != data_version:
        raise C.StepError(f"the gate report is for data version {report.get('data_version')!r}, "
                          f"not the pulled {data_version!r}")
    reload = C.read_object(artifacts / "reload_check.json")  # written only when the reload check passed
    if reload.get("model") != evaluation.get("winner"):
        raise C.StepError(f"the reload check is for {reload.get('model')!r}, not the winner "
                          f"{evaluation.get('winner')!r}")
    folder = settings.runs_dir / str(evaluation.get("winner_run"))
    if not (folder / "model.joblib").is_file():
        raise C.StepError(f"{folder / 'model.joblib'} is missing")
    return evaluation, report, data_version


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(DESCRIPTION, argv, with_config=True)
    import registry

    settings = C.load_settings(config)
    try:
        registry_settings = registry.load_settings()
    except registry.RegistryError as exc:
        raise C.StepError(f"cannot load the registry settings: {exc}") from exc
    evaluation, report, data_version = checked_inputs(artifacts, settings)
    image_text, digest = read_image_digest(artifacts, registry_settings.serving_image)
    commit = os.environ.get("GITHUB_SHA", "").strip() or None
    if commit is None:
        if C.main_run():
            raise C.StepError("GITHUB_SHA is not set: a registered version must name its commit")
        print("::warning::GITHUB_SHA is not set: the push takes the commit from run.json, if any")

    training = ["--training-config", str(config)] if config else []
    out = artifacts / "push.json"
    C.registry_main([*training, "push", "--gate-report", str(artifacts / "gate_report.json"),
                     "--evaluation", str(artifacts / "evaluation.json"), "--out", str(out),
                     *(["--image-digest", digest] if digest else [])], "push")

    record = C.read_object(out)
    folder = settings.runs_dir / str(evaluation["winner_run"])
    run = C.read_object(folder / "run.json")
    splits = (run.get("settings") or {}).get("splits") or {}
    pushed = "skipped" not in record
    metadata = record.get("metadata") or {}
    C.write_json(artifacts / "registry.json", {
        "model_version": record.get("version") if pushed else None,
        "pushed": pushed,
        **({} if pushed else {"skipped": record["skipped"]}),
        "model": {"name": evaluation["winner"], "path": f"runs/{folder.name}/model.joblib",
                  "sha256": hashlib.sha256((folder / "model.joblib").read_bytes()).hexdigest()},
        "data_version": data_version,
        "git_commit": metadata.get("git_commit", commit),
        "mlflow_run_id": metadata.get("mlflow_run_id", (run.get("mlflow") or {}).get("run_id")),
        "image_digest": image_text,
        "gates_sha256": report.get("gates_sha256"),
        "fairness_group_sha256": report.get("fairness_group_sha256"),
        "windows": {name: {"first": window[0], "last": window[1]} for name, window in splits.items()},
        "decision": report.get("decision"),
        "files_uri": record.get("files_uri"),
    })


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
