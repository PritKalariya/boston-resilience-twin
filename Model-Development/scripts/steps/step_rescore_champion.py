"""Step: re-score the champion on the challenger's data version (#112, ADR-028). STUB. Main only.

PR 3 replaces the body with #111's champion lookup and re-score; it fills the
`champion` block of `metrics.json` (tp, fp, fn, data_version, model_version). The
stub finds no champion and leaves `champion` null. Output: `champion_rescore.json`.
"""

from __future__ import annotations

import sys

import step_common as C


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    C.stub_outcome()
    path = artifacts / "metrics.json"
    metrics = C.read_json(path)
    if not isinstance(metrics, dict):
        raise C.StepError(f"{path} is not a JSON object")
    metrics["champion"] = None
    C.write_json(path, metrics)
    C.write_json(
        artifacts / "champion_rescore.json",
        {"champion": None, "reason": "stub: no champion in the registry"},
    )


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
