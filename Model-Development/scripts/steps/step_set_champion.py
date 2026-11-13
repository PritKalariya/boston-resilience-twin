"""INTERFACE FOR #111 (Kirtan): replace this body; keep the command line and exit codes.
Called as: python Model-Development/scripts/steps/step_set_champion.py --artifacts DIR
Reads: `gate_report.json` (`decision`) and `registry.json` (`model_version`).
Writes: with the decision `promote`, `champion.json` (the stub: `action` "would set champion" and
  `model_version`; the real step sets the Vertex AI Model Registry champion alias); with `keep_champion`,
  nothing.
Exit: 0 ok, 2 pipeline failed
Must hold: only the decision `promote` changes the champion; `keep_champion` changes nothing; any other
  decision (`blocked`, `error`) never reaches this step and exits 2.

Step: set the champion from the gate decision (#112, ADR-028, ADR-032). STUB. Main only.
"""

from __future__ import annotations

import sys

import step_common as C


DESCRIPTION = "Step: set the champion from the gate decision"


def step(argv: list[str] | None) -> None:
    artifacts, _ = C.parse_artifacts(DESCRIPTION, argv)
    report = C.read_json(artifacts / "gate_report.json")
    decision = report.get("decision") if isinstance(report, dict) else None
    if decision == "promote":
        registry = C.read_json(artifacts / "registry.json")
        version = registry.get("model_version") if isinstance(registry, dict) else None
        C.write_json(artifacts / "champion.json", {"action": "would set champion", "model_version": version})
        print(f"would set champion: {version}")
    elif decision == "keep_champion":
        print("keep champion: nothing changed")
    else:
        raise C.StepError(f"gate decision {decision!r} cannot set a champion")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
