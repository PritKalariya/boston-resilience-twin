"""INTERFACE FOR #110 (Alekhya): replace this body; keep the command line and exit codes.
Called as: python Model-Development/scripts/steps/step_evaluate.py --artifacts DIR
Reads: `data_version.txt`; for the baseline (gates.csv `f1_baseline`) and each candidate of training.csv,
  `runs_dir/ci-<model>/validation_counts.json` (`data_version`, `tp`, `fp`, `fn`) and the `validation` block
  of `metrics.json` (`tp`, `fp`, `fn`, `log_loss`, for the ranking only); the winner's
  `runs_dir/ci-<model>/predictions.parquet`, validation rows only (`unit`, `label`, `prediction`);
  `Model-Development/config/poverty_groups.csv` (`neighborhood`, `group`), the file gates.csv names and
  checks by sha256.
Writes: `metrics.json` for gates.py: `data_version`; `candidate` and `baseline` as `{tp, fp, fn}`;
  `champion` null; `groups` = `{<group name from gates.csv fairness_groups>: {positives, true_positives}}`;
  `excluded` = `{<neighborhood>: {positives, true_positives, reason}}`. Also `evaluation.json`: the winner,
  its run folder, the ranking by validation F1 and the baseline's name.
Exit: 0 ok, 2 pipeline failed
Must hold: `groups` has exactly the gates.csv fairness_groups; positives is the validation surge days
  (label 1) of a group's neighborhoods and true_positives those among them with prediction 1;
  groups plus excluded positives equal the candidate's tp + fn, and their true positives equal the
  candidate's tp; every neighborhood of the validation rows is a group member or in `excluded`
  (ungrouped neighborhoods carry reason "ungrouped"); the baseline's tp + fn equals the candidate's;
  `data_version` is the same in every run and in `data_version.txt`.

Step: pick the winner and write the gates' input from the validation split (#112, ADR-028, ADR-032).
INTERIM: the per-group counts here are computed from the winner's validation predictions until
#110's bias analysis replaces them.

The winner is the first candidate by selection.py's ranking: validation F1 (exact), then validation log
loss, then the name (`selection.rank`). selection.py's margin check is not applied: gates.py alone
decides whether a model is promoted (ADR-032). Only the validation split is read: the test block of
metrics.json is dropped on reading and the test rows of predictions.parquet are filtered out in the
parquet read, so no test metric enters this step (Xu 2024, Domingos 2012).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import step_common as C

DESCRIPTION = "Step: pick the winner and write the gates' input from the validation split"


def read_poverty_groups(path: Path, configured: tuple[str, ...]) -> dict[str, str]:
    """neighborhood -> group name or "ungrouped", from the file gates.csv names (already checked by gates.load_gates)."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise C.StepError(f"cannot read {path}: {exc}") from exc
    mapping = {row["neighborhood"].strip(): row["group"].strip() for row in rows}
    unknown = sorted(set(mapping.values()) - set(configured) - {"ungrouped"})
    if unknown:
        raise C.StepError(f"{path.name} has groups {unknown} that gates.csv does not name")
    return mapping


def group_counts(validation, mapping: dict[str, str], configured: tuple[str, ...]) -> tuple[dict, dict]:
    """(groups, excluded) for the gates: positives and true positives of the winner's validation rows per
    configured group, and per ungrouped neighborhood. A neighborhood in no list fails the step."""
    strangers = sorted(set(validation["unit"]) - set(mapping))
    if strangers:
        raise C.StepError(f"validation neighborhoods {strangers} are not in poverty_groups.csv, so no group "
                          "or exclusion covers their surge days")
    surge = validation["label"].astype(int) == 1
    caught = surge & (validation["prediction"].astype(int) == 1)
    groups = {}
    for name in configured:
        members = validation["unit"].map(mapping) == name
        groups[name] = {"positives": int((surge & members).sum()), "true_positives": int((caught & members).sum())}
    excluded = {}
    for hood in sorted(h for h, group in mapping.items() if group == "ungrouped"):
        rows = validation["unit"] == hood
        excluded[hood] = {"positives": int((surge & rows).sum()), "true_positives": int((caught & rows).sum()),
                          "reason": "ungrouped"}
    return groups, excluded


def check_reconciliation(groups: dict, excluded: dict, candidate: dict) -> None:
    """The two rules gates.py checks, here first so a mismatch names the step that broke it."""
    parts = [*groups.values(), *excluded.values()]
    positives = sum(part["positives"] for part in parts)
    caught = sum(part["true_positives"] for part in parts)
    if positives != candidate["tp"] + candidate["fn"]:
        raise C.StepError(f"group plus excluded positives {positives} do not equal the candidate's "
                          f"tp + fn {candidate['tp'] + candidate['fn']}")
    if caught != candidate["tp"]:
        raise C.StepError(f"group plus excluded true positives {caught} do not equal the candidate's tp {candidate['tp']}")


def read_run(runs_dir: Path, model: str) -> tuple[dict, dict]:
    """(validation_counts.json, the validation block of metrics.json) of a model's workflow run.
    The test block of metrics.json is dropped on reading and never looked at."""
    folder = runs_dir / C.run_id(model)
    counts = C.read_object(folder / "validation_counts.json")
    validation = C.read_object(folder / "metrics.json").get("validation")
    if not isinstance(validation, dict):
        raise C.StepError(f"{folder / 'metrics.json'} has no validation block")
    for key in ("tp", "fp", "fn"):
        if not isinstance(counts.get(key), int) or isinstance(counts.get(key), bool):
            raise C.StepError(f"{folder / 'validation_counts.json'} has no integer {key}")
    return counts, validation


def pick_winner(entries: dict, candidates: tuple[str, ...]) -> list:
    """The candidates best first by selection.py's ranking (selection.rank): exact validation F1, then
    validation log loss, then the name. No margin: gates.py decides."""
    import selection

    return selection.rank([entries[name] for name in candidates])


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(DESCRIPTION, argv, with_config=True)
    import pandas as pd
    import gates
    import selection

    settings = C.load_settings(config)
    try:
        loaded = gates.load_gates()
    except (OSError, ValueError) as exc:
        raise C.StepError(f"cannot load the gates: {exc}") from exc
    version = C.read_data_version(artifacts)
    baseline_name = loaded.f1_baseline

    counts, entries = {}, {}
    for model in (baseline_name, *settings.candidates):
        counts[model], validation = read_run(settings.runs_dir, model)
        if counts[model].get("data_version") != version:
            raise C.StepError(f"{model} was trained on {counts[model].get('data_version')!r}, "
                              f"not the pulled data version {version!r}")
        entries[model] = selection.Entry(model, settings.runs_dir / C.run_id(model), {}, {"validation": validation})
    ranked = pick_winner(entries, settings.candidates)
    winner = ranked[0]
    candidate = {key: counts[winner.model][key] for key in ("tp", "fp", "fn")}
    baseline = {key: counts[baseline_name][key] for key in ("tp", "fp", "fn")}

    rows = pd.read_parquet(entries[winner.model].run_dir / "predictions.parquet",
                           columns=["unit", "label", "prediction"], filters=[("split", "==", "validation")])
    if rows.empty:
        raise C.StepError(f"{winner.model}'s predictions have no validation rows")
    mapping = read_poverty_groups(gates.REPO_ROOT / loaded.fairness_group_source, loaded.fairness_groups)
    groups, excluded = group_counts(rows, mapping, loaded.fairness_groups)
    check_reconciliation(groups, excluded, candidate)

    C.write_json(artifacts / "metrics.json", {"data_version": version, "candidate": candidate, "baseline": baseline,
                                              "champion": None, "groups": groups, "excluded": excluded})
    C.write_json(artifacts / "evaluation.json", {
        "winner": winner.model, "winner_run": C.run_id(winner.model), "baseline": baseline_name,
        "ranking": [{"model": e.model, "run": C.run_id(e.model), "validation_f1": None if e.f1 is None else str(e.f1)}
                    for e in ranked],
        "rule": "validation F1 (exact), then validation log loss, then name; no margin (gates.py decides)"})
    print(f"winner {winner.model}: validation F1 {'n/a' if winner.f1 is None else f'{float(winner.f1):.4f}'}; "
          f"baseline {baseline_name} {'n/a' if entries[baseline_name].f1 is None else f'{float(entries[baseline_name].f1):.4f}'}")
    for name, count in groups.items():
        print(f"group {name}: {count['true_positives']} of {count['positives']} surge days caught")
    for hood, count in excluded.items():
        print(f"excluded {hood}: {count['true_positives']} of {count['positives']} ({count['reason']})")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
