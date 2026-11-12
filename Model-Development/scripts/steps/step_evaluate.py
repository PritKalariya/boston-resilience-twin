"""Step: pick the winner after the bias check and write the gates' input from the validation split
(#110, #112, ADR-028, ADR-032, ADR-033).

Called as: python Model-Development/scripts/steps/step_evaluate.py --artifacts DIR
Reads: `data_version.txt`; for the baseline (gates.csv `f1_baseline`) and each candidate of training.csv,
  `runs_dir/ci-<model>/validation_counts.json` (`data_version`, `tp`, `fp`, `fn`) and the `validation` block
  of `metrics.json` (`tp`, `fp`, `fn`, `log_loss`, for the ranking only); each candidate's
  `runs_dir/ci-<model>/predictions.parquet`, validation rows only; `Model-Development/config/poverty_groups.csv`
  (the file gates.csv names and checks by sha256) and `slice_groups.csv` (bias.py); for the winner's report,
  `features.parquet` (the raw 311 lags and the population, for the reporting bias).
Writes: `metrics.json` for gates.py: `data_version`; `candidate` and `baseline` as `{tp, fp, fn}`;
  `champion` null; `groups` = `{<group name from gates.csv fairness_groups>: {positives, true_positives}}`;
  `excluded` = `{<neighborhood>: {positives, true_positives, reason}}`. `evaluation.json`: the winner, its run
  folder, the ranking by validation F1, each candidate's bias check, how the winner was chosen and the
  baseline's name. `bias/`: the winner's bias report on validation (bias.py's four files), also logged to
  its MLflow run.
Exit: 0 ok, 2 pipeline failed
Must hold: `groups` has exactly the gates.csv fairness_groups; positives is the validation surge days
  (label 1) of a group's neighborhoods and true_positives those among them with prediction 1;
  groups plus excluded positives equal the candidate's tp + fn, and their true positives equal the
  candidate's tp; every neighborhood of the validation rows is a group member or in `excluded`
  (ungrouped neighborhoods carry reason "ungrouped"); the baseline's tp + fn equals the candidate's;
  `data_version` is the same in every run and in `data_version.txt`.

The candidates are ranked by selection.py's ranking: validation F1 (exact), then validation log loss, then
the name (`selection.rank`). Selection after bias (#110): the winner is the first ranked candidate whose
fairness gate passes (bias.bias_check, which is gates.evaluate's own fairness gate); when none passes, the
top-ranked candidate is kept, so gates.py blocks it and the run reports why. selection.py's margin check
is not applied: gates.py alone decides whether a model is promoted (ADR-032).

Only the validation split is read: the test block of metrics.json is dropped on reading, the test rows of
predictions.parquet are filtered out, and the winner's report covers validation only, so no test number
enters this step (Xu 2024, Domingos 2012). The test-split bias report is bias.py's, run on the selected
model's run folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import step_common as C

DESCRIPTION = "Step: pick the winner after the bias check and write the gates' input from the validation split"
BIAS_DIR = "bias"


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


def validation_rows(run_dir: Path):
    import pandas as pd

    rows = pd.read_parquet(run_dir / "predictions.parquet", columns=["unit", "split", "label", "prediction"],
                           filters=[("split", "==", "validation")])
    if rows.empty:
        raise C.StepError(f"{run_dir.name}'s predictions have no validation rows")
    return rows


def pick_after_bias(ranked: list, checks: dict) -> tuple[object, str]:
    """The first ranked candidate whose fairness gate passes, or the top one when none does."""
    for entry in ranked:
        if checks[entry.model]["pass"]:
            if entry is ranked[0]:
                return entry, f"{entry.model}: best validation F1 and its fairness gate passes"
            failed = ", ".join(e.model for e in ranked[:ranked.index(entry)])
            return entry, f"{entry.model}: the best validation F1 whose fairness gate passes ({failed} failed it)"
    return ranked[0], f"{ranked[0].model}: no candidate passes the fairness gate, so the best F1 is kept and gates.py blocks it"


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(DESCRIPTION, argv, with_config=True)
    import bias
    import gates
    import selection

    settings = C.load_settings(config)
    try:
        loaded = gates.load_gates()
        groups = bias.load_groups(loaded)
    except (OSError, ValueError) as exc:
        raise C.StepError(f"cannot load the gates or the bias groups: {exc}") from exc
    mapping = groups["poverty"].to_dict()
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

    checks, rows = {}, {}
    for entry in ranked:
        rows[entry.model] = validation_rows(entry.run_dir)
        try:
            checks[entry.model] = bias.bias_check(rows[entry.model], loaded, mapping, version)
        except bias.BiasError as exc:
            raise C.StepError(f"{entry.model}: {exc}") from exc
        if checks[entry.model]["counts"]["candidate"] != {key: counts[entry.model][key] for key in ("tp", "fp", "fn")}:
            raise C.StepError(f"{entry.model}: its predictions' validation counts differ from validation_counts.json")
    winner, why = pick_after_bias(ranked, checks)
    counted = checks[winner.model]["counts"]
    baseline = {key: counts[baseline_name][key] for key in ("tp", "fp", "fn")}

    try:
        report, files = bias.analyse(winner.run_dir, settings.features, settings.splits, artifacts / BIAS_DIR,
                                     loaded=loaded, report_splits=("validation",), rows=rows[winner.model])
        bias.log_files(winner.run_dir, files, settings)
    except (bias.BiasError, OSError, ValueError, KeyError) as exc:
        raise C.StepError(f"the bias report for {winner.model} failed: {exc}") from exc

    C.write_json(artifacts / "metrics.json", {"data_version": version, "candidate": counted["candidate"],
                                              "baseline": baseline, "champion": None, "groups": counted["groups"],
                                              "excluded": counted["excluded"]})
    C.write_json(artifacts / "evaluation.json", {
        "winner": winner.model, "winner_run": C.run_id(winner.model), "baseline": baseline_name,
        "ranking": [{"model": e.model, "run": C.run_id(e.model), "validation_f1": None if e.f1 is None else str(e.f1),
                     "fairness_gate": {"pass": checks[e.model]["pass"], "gap": checks[e.model]["gap"],
                                       "worst_group": checks[e.model]["worst_group"]}} for e in ranked],
        "selected": why, "bias_report": f"{BIAS_DIR}/bias_report.md", "reopen_conditions": report["triggers"],
        "rule": "validation F1 (exact), then validation log loss, then name, the first whose fairness gate passes; "
                "no margin (gates.py decides)"})
    print(f"winner {why}; validation F1 {'n/a' if winner.f1 is None else f'{float(winner.f1):.4f}'}; baseline "
          f"{baseline_name} {'n/a' if entries[baseline_name].f1 is None else f'{float(entries[baseline_name].f1):.4f}'}")
    for entry in ranked:
        check = checks[entry.model]
        print(f"bias {entry.model}: gap {check['gap']:.3f} ({'pass' if check['pass'] else 'FAIL'})")
    for name, count in counted["groups"].items():
        print(f"group {name}: {count['true_positives']} of {count['positives']} surge days caught")
    for hood, count in counted["excluded"].items():
        print(f"excluded {hood}: {count['true_positives']} of {count['positives']} ({count['reason']})")
    for item in report["triggers"]:
        print(f"ADR-026 reopen condition: {item['condition']}: {item.get('neighborhood', item.get('recalls'))}")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
