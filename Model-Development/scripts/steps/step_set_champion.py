"""Step: set the champion from the gate decision (#111, #112, ADR-006, ADR-028, ADR-032). Main only.

Called as: python Model-Development/scripts/steps/step_set_champion.py --artifacts DIR
Reads: `gate_report.json` (`decision`) and `push.json` (the registry push step's record).
Writes: `promotion.json`, the record of `registry.py promote` (or `{"skipped": ...}`); with the decision
  `promote`, also `champion.json` (`action` "set champion", `model_version`, `previous`), after the
  `production` alias moved to the pushed version and `previous` to the old champion; with `keep_champion`,
  no `champion.json`.
Exit: 0 ok, 2 pipeline failed
Must hold: only the decision `promote` changes the champion; `keep_champion` changes nothing; any other
  decision (`blocked`, `error`) never reaches this step and exits 2.

This is `registry.py promote` (#111), the rollback rule: it also refuses a gate report that compared with
another champion than the version holding `production`, or that is not the one the push uploaded. On main
(`MAIN_RUN=true`) no project 2 access or a skipped push fails the step; elsewhere it is skipped with a log
line. Credentials come from the file in_image.sh mounts (ADR-035).
"""

from __future__ import annotations

import sys

import step_common as C

DESCRIPTION = "Step: set the champion from the gate decision"


def step(argv: list[str] | None) -> None:
    artifacts, _ = C.parse_artifacts(DESCRIPTION, argv)
    report = C.read_json(artifacts / "gate_report.json")
    decision = report.get("decision") if isinstance(report, dict) else None
    if decision not in ("promote", "keep_champion"):
        raise C.StepError(f"gate decision {decision!r} cannot set a champion")
    C.read_object(artifacts / "push.json")  # the push step ran
    out = artifacts / "promotion.json"
    C.registry_main(["promote", "--push-record", str(artifacts / "push.json"),
                     "--gate-report", str(artifacts / "gate_report.json"), "--out", str(out)], "promote")
    record = C.read_object(out)
    if "skipped" in record:
        print(f"set champion skipped: {record['skipped']}")
    elif record.get("decision") == "promote":
        C.write_json(artifacts / "champion.json", {"action": "set champion", "model_name": record["model_name"],
                                                   "model_version": record["champion"],
                                                   "previous": record.get("previous")})
        print(f"set champion: version {record['champion']} (previous: {record.get('previous')})")
    else:
        print(f"keep champion: version {record.get('champion')} stays, nothing changed")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
