"""Step: reload the saved model and check it scores like the trained one (#112, ADR-028). STUB. Main only.

Reloads `model/model.json`, scores the fixture row (`config/reload_fixture_row.json`)
and compares with `train_scores.json` at an absolute tolerance of 1e-9. A mismatch
exits 2. PR 3 replaces the loader with the real model's. STUB_OUTCOME=fail_pipeline
forces a mismatch.
"""

from __future__ import annotations

import sys

import step_common as C

TOLERANCE = 1e-9
FORCED_DRIFT = 0.01


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    outcome = C.stub_outcome()
    model = C.load_model(artifacts / "model" / "model.json")
    row = C.read_json(C.FIXTURE_ROW)
    trained = C.read_json(artifacts / "train_scores.json")
    try:
        expected = float(trained["fixture_row_score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise C.StepError("train_scores.json has no fixture_row_score") from exc
    reloaded = C.score(model, row)
    if outcome == "fail_pipeline":
        reloaded += FORCED_DRIFT
    if abs(reloaded - expected) > TOLERANCE:
        raise C.StepError(
            f"reload mismatch: trained {expected!r}, reloaded {reloaded!r} (tolerance {TOLERANCE})"
        )
    print(f"reload check ok: {reloaded!r} matches {expected!r}")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
