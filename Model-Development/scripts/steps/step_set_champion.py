"""Step: set the champion from the gate decision (#112, ADR-028). STUB. Main only.

Reads `gate_report.json`. `promote` writes `champion.json` saying "would set champion"
(PR 3 calls #111's registry alias change). `keep_champion` changes nothing. Any other
decision should never reach this step and exits 2.
"""

from __future__ import annotations

import sys

import step_common as C


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    C.stub_outcome()
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
