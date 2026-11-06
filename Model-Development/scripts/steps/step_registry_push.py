"""Step: register the model (#112, ADR-028). STUB. Main only. No network.

PR 3 replaces the body with #111's Vertex AI Model Registry push, storing the gate
hashes with the model. The stub writes `registry.json` with a stub model version and the
image digest (#111) from `image_digest.txt`, which the workflow writes right after the
build: the local image ID until PR 3 pushes to Artifact Registry, then the registry digest.
A missing file is a warning, not a failure: `image_digest` is null.
"""

from __future__ import annotations

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


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    C.stub_outcome()
    C.load_model(artifacts / "model" / "model.json")
    data_version = C.read_data_version(artifacts)
    report = C.read_json(artifacts / "gate_report.json")
    if not isinstance(report, dict):
        raise C.StepError("gate_report.json is not a JSON object")
    C.write_json(
        artifacts / "registry.json",
        {
            "model_version": f"stub-model-{data_version}",
            "pushed": False,
            "data_version": data_version,
            "image_digest": read_image_digest(artifacts),
            "gates_sha256": report.get("gates_sha256"),
            "fairness_group_sha256": report.get("fairness_group_sha256"),
        },
    )


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
