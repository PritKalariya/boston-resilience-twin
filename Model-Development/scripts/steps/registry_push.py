"""Step: register the model (#112, ADR-028). STUB. Main only. No network.

PR 3 replaces the body with #111's Vertex AI Model Registry push, storing the gate
hashes with the model. The stub writes `registry.json` with a stub model version.
"""

from __future__ import annotations

import sys

import _common as C


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
            "gates_sha256": report.get("gates_sha256"),
            "fairness_group_sha256": report.get("fairness_group_sha256"),
        },
    )


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
