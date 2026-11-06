"""Step: score the candidate and the baseline on the validation split (#112, ADR-028). STUB.

Writes `metrics.json` in the input shape of gates.evaluate(): tp, fp and fn for the
candidate and the seasonal baseline, per-group and excluded counts that reconcile with
the candidate's tp and fn, and a null champion.

#106 merged (#128) with `train.py` and `evaluate.py` and the seasonal baseline, but this
stub does not call them yet. The baseline needs the versioned data, which needs the DVC
pull through M2's federation, and that arrives in PR 3. Until then the counts are fixture
numbers. PR 3 fills `metrics.json` from #128's output (ADR-027), one run per model, each
with `train.py --model <name>` on the same data version:
  - `candidate` (tp, fp, fn) from the candidate run's `validation_counts.json` (`tp`, `fp`,
    `fn`);
  - `baseline` (tp, fp, fn) from the `--model seasonal` run's `validation_counts.json`;
  - top-level `data_version` from `validation_counts.json`'s `data_version`, which is the
    same in both runs (the gates need the two to match);
  - `groups` and `excluded` from #110's per-group counts: `validation_counts.json` has none.

STUB_OUTCOME=pass gives a promote. fail_gate gives a worst-group recall gap of 0.6,
over the 0.15 gate. fail_pipeline behaves like pass here and fails at reload_check.
"""

from __future__ import annotations

import sys

import step_common as C

CANDIDATE = {"tp": 60, "fp": 20, "fn": 40}  # F1 0.667
BASELINE = {"tp": 40, "fp": 40, "fn": 60}  # F1 0.444, margin 0.222 over the 0.05 gate

GROUPS_PASS = {  # recall 0.6 and 0.6: gap 0
    "lower": {"positives": 50, "true_positives": 30},
    "higher": {"positives": 50, "true_positives": 30},
}
GROUPS_FAIL_GATE = {  # recall 0.9 and 0.3: gap 0.6; same totals, so the counts still reconcile
    "lower": {"positives": 50, "true_positives": 45},
    "higher": {"positives": 50, "true_positives": 15},
}
EXCLUDED = {"Harbor Islands": {"positives": 0, "true_positives": 0, "reason": "ungrouped"}}


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    outcome = C.stub_outcome()
    data_version = C.read_data_version(artifacts)
    C.load_model(artifacts / "model" / "model.json")  # the candidate must exist
    groups = GROUPS_FAIL_GATE if outcome == "fail_gate" else GROUPS_PASS
    metrics = {
        "data_version": data_version,
        "candidate": dict(CANDIDATE),
        "baseline": dict(BASELINE),
        "champion": None,
        "groups": {name: dict(counts) for name, counts in groups.items()},
        "excluded": {name: dict(counts) for name, counts in EXCLUDED.items()},
    }
    C.write_json(artifacts / "metrics.json", metrics)


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
