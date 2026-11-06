"""Step: train the candidate model (#112, ADR-028). STUB.

PR 3 replaces the body with a call into the trainer (#108 or M4). Outputs:
`model/model.json` (here a constant-score model) and `train_scores.json`, the
model's score on the reload fixture row, which `step_reload_check.py` compares against.
"""

from __future__ import annotations

import sys

import step_common as C

CONSTANT_SCORE = 0.3721


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    C.stub_outcome()
    C.read_data_version(artifacts)
    model = {"type": "constant", "score": CONSTANT_SCORE}
    C.write_json(artifacts / "model" / "model.json", model)
    row = C.read_json(C.FIXTURE_ROW)
    C.write_json(artifacts / "train_scores.json", {"fixture_row_score": C.score(model, row)})


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
