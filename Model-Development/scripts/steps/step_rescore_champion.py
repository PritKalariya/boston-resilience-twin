"""INTERFACE FOR #111 (Kirtan): replace this body; keep the command line and exit codes.
Called as: python Model-Development/scripts/steps/step_rescore_champion.py --artifacts DIR
Reads: `metrics.json` (the evaluate step's) and `data_version.txt`; with a registry, the champion model
  (Vertex AI Model Registry alias) and the validation rows of the challenger's data version.
Writes: `metrics.json` with `champion` set to `{tp, fp, fn, data_version, model_version}` (null with no
  champion), and `champion_rescore.json` saying what was done.
Exit: 0 ok, 2 pipeline failed
Must hold: the champion's tp + fn equals the candidate's (the same validation rows), and its
  `data_version` equals the top-level `data_version` (gates.py returns "error" otherwise, ADR-026);
  the champion is scored with the same shared threshold rule, on the challenger's data version.

Step: re-score the champion on the challenger's data version (#112, ADR-028, ADR-032). STUB. Main only.

The stub has no registry, so `champion` is null: the candidate is judged against the baseline alone.
"""

from __future__ import annotations

import sys

import step_common as C


DESCRIPTION = "Step: re-score the champion on the challenger's data version"


def step(argv: list[str] | None) -> None:
    artifacts, _ = C.parse_artifacts(DESCRIPTION, argv)
    metrics = C.read_object(artifacts / "metrics.json")
    metrics["champion"] = None
    C.write_json(artifacts / "metrics.json", metrics)
    C.write_json(artifacts / "champion_rescore.json",
                 {"champion": None, "reason": "stub: no champion in the registry (#111)"})


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
