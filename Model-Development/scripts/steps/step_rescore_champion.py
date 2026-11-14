"""Step: re-score the champion on the challenger's data version (#111, #112, ADR-006, ADR-028, ADR-032). Main only.

Called as: python Model-Development/scripts/steps/step_rescore_champion.py --artifacts DIR
Reads: `metrics.json` (the evaluate step's) and `data_version.txt`; the version holding the `production`
  alias in project 2's Vertex AI Model Registry, its files in the model bucket, and the validation rows of
  the mounted data (the challenger's data version).
Writes: `metrics.json` with `champion` set to `{tp, fp, fn, data_version, model_version}` (null with no
  champion), and `champion_rescore.json` saying what was done (`registry.py rescore --out`).
Exit: 0 ok, 2 pipeline failed
Must hold: the champion's tp + fn equals the candidate's (the same validation rows), and its
  `data_version` equals the top-level `data_version` (gates.py returns "error" otherwise, ADR-026);
  the champion is scored with the same shared threshold rule, on the challenger's data version.

This is `registry.py rescore` (#111) with the workflow's files. Credentials come from the file in_image.sh
mounts (ADR-035). On main (`MAIN_RUN=true`) no project 2 access fails the step; elsewhere the champion
stays null and a log line says so.
"""

from __future__ import annotations

import sys

import step_common as C

DESCRIPTION = "Step: re-score the champion on the challenger's data version"


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(DESCRIPTION, argv, with_config=True)
    metrics = C.read_object(artifacts / "metrics.json")
    version = C.read_data_version(artifacts)
    if metrics.get("data_version") != version:
        raise C.StepError(f"metrics.json is for data version {metrics.get('data_version')!r}, "
                          f"not the pulled {version!r}")
    training = ["--training-config", str(config)] if config else []
    C.registry_main([*training, "rescore", "--metrics", str(artifacts / "metrics.json"),
                     "--out", str(artifacts / "champion_rescore.json")], "rescore")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
