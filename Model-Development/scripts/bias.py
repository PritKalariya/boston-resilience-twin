"""Model bias (#110, ADR-033): per-group results of a training run, the fairness gate and its triggers.

    python Model-Development/scripts/bias.py --run Model-Development/runs/<run id> [--out-dir DIR] [--no-mlflow]

The gated groups are the frozen ACS poverty groups of ADR-026 (poverty_groups.csv, which gates.csv names
and checks by sha256). bias_check() turns a run's validation predictions into the per-group counts
gates.evaluate() takes and returns its fairness gate: the worst-group recall gap against
fairness_max_gap, with gates.csv's floor and pooling. The floor, the limit and the groups come from
gates.csv and poverty_groups.csv; this module has no constants of its own for them.

Reported, not gated (slice_groups.csv, written by #32's bias_slicing.py --model-groups on the host, since the
training image has neither geopandas nor the static layers):
- the other #32 families: people of color, limited English and flood zone, each split at the median
  neighborhood as #32 does;
- each neighborhood;
- the higher-poverty group in two parts, its student areas and the rest (#126, #32's rule: at least
  twice the city's share of college and graduate students, ACS B14002). A diagnostic, not a gate.

For every family, split (validation and test) and group: surges, true positives, recall, precision and
F1, from Fairlearn's MetricFrame. A group with fewer surges than gates.csv's fairness_min_group_surges
is merged with the family's other small groups into one, as the gate pools them.

It also checks ADR-026's two "Reopen if" conditions on validation: a neighborhood with at least the
floor of surges whose recall is more than fairness_max_gap below its own poverty group's, and the
higher group's two parts differing by more than fairness_max_gap. A trigger does not block promotion:
it is reported for the gate's owner (ADR-026).

The report also states the residual reporting bias (#32, ADR-025): storm requests per 1,000 residents a
year by poverty group, from the raw 311 lags and the population in features.parquet.

Writes bias.json, bias_slices.csv, bias_report.md and bias_recall.png to --out-dir (default: the run
folder) and, unless --no-mlflow, logs them to the run's MLflow run. Exits 0 when the gate passes, 1
when it fails and 2 when it cannot be evaluated, as gates.py does.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import gates

SCRIPTS = Path(__file__).resolve().parent
CONFIG = SCRIPTS.parent / "config"
SLICE_GROUPS = CONFIG / "slice_groups.csv"
GROUPS_HEADER = ["neighborhood", "poc", "limited_english", "flood_zone", "student_area"]
FAMILIES = ["poverty", "poc", "limited_english", "flood_zone"]  # poverty is the gated one
NEIGHBORHOOD = "neighborhood"
TWO_PARTS = "poverty_higher_by_student_area"  # the #126 diagnostic
STUDENT = "student area"
SPLITS = ("validation", "test")
UNGROUPED = gates.UNGROUPED
LAGS = ("snow_ice_last", "tree_last")  # raw 311 counts on D - 2 (feature_table.py), kept out of the model
POPULATION = "static_population"
YEAR_DAYS = 365.25


class BiasError(ValueError):
    """A run or a groups file the bias analysis cannot use."""


# Groups


def load_groups(loaded: gates.Gates, path: str | Path = SLICE_GROUPS) -> pd.DataFrame:
    """Every neighborhood's group in each family, indexed by neighborhood: poverty from the file
    gates.csv names, the others from slice_groups.csv. Both must list the same neighborhoods."""
    poverty = read_poverty_groups(gates.REPO_ROOT / loaded.fairness_group_source, loaded.fairness_groups)
    path = Path(path)
    try:
        table = pd.read_csv(path, dtype=str, keep_default_na=False)
    except OSError as exc:
        raise BiasError(f"cannot read {path}: {exc}") from exc
    if list(table.columns) != GROUPS_HEADER:
        raise BiasError(f"{path.name} needs the header {','.join(GROUPS_HEADER)}")
    table = table.apply(lambda column: column.str.strip())
    if (table == "").any().any():
        raise BiasError(f"{path.name} has a blank cell")
    if table["neighborhood"].duplicated().any():
        raise BiasError(f"{path.name} repeats a neighborhood")
    table = table.set_index("neighborhood")
    if set(table.index) != set(poverty):
        raise BiasError(f"{path.name} and {Path(loaded.fairness_group_source).name} list different neighborhoods: "
                        f"{sorted(set(table.index) ^ set(poverty))}")
    table.insert(0, "poverty", pd.Series(poverty))
    return table.sort_index()


def read_poverty_groups(path: Path, configured: tuple[str, ...]) -> dict[str, str]:
    """neighborhood -> group name or "ungrouped", from the file gates.csv names (gates.load_gates checks it)."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise BiasError(f"cannot read {path}: {exc}") from exc
    mapping = {row["neighborhood"].strip(): row["group"].strip() for row in rows}
    unknown = sorted(set(mapping.values()) - set(configured) - {UNGROUPED})
    if unknown:
        raise BiasError(f"{path.name} has groups {unknown} that gates.csv does not name")
    return mapping


# The gate


def gate_counts(rows: pd.DataFrame, mapping: dict[str, str], configured: tuple[str, ...]) -> tuple[dict, dict]:
    """(groups, excluded) in the shape gates.evaluate() takes: surge days (label 1) and caught ones
    (prediction 1 among them) per configured group, and per ungrouped neighborhood. rows are one split's
    predictions; a neighborhood in no list fails, so no surge day goes uncounted."""
    strangers = sorted(set(rows["unit"]) - set(mapping))
    if strangers:
        raise BiasError(f"neighborhoods {strangers} are not in the poverty groups file, so no group or "
                        "exclusion covers their surge days")
    surge = rows["label"].astype(int) == 1
    caught = surge & (rows["prediction"].astype(int) == 1)
    group = rows["unit"].map(mapping)
    groups = {name: {"positives": int((surge & (group == name)).sum()),
                     "true_positives": int((caught & (group == name)).sum())} for name in configured}
    excluded = {}
    for hood in sorted(h for h, name in mapping.items() if name == UNGROUPED):
        here = rows["unit"] == hood
        excluded[hood] = {"positives": int((surge & here).sum()), "true_positives": int((caught & here).sum()),
                          "reason": UNGROUPED}
    return groups, excluded


def confusion(rows: pd.DataFrame) -> dict:
    label, prediction = rows["label"].astype(int), rows["prediction"].astype(int)
    return {"tp": int(((label == 1) & (prediction == 1)).sum()), "fp": int(((label == 0) & (prediction == 1)).sum()),
            "fn": int(((label == 1) & (prediction == 0)).sum())}


def check_reconciliation(groups: dict, excluded: dict, candidate: dict) -> None:
    """The two rules gates.py checks, here first so a mismatch names this step."""
    parts = [*groups.values(), *excluded.values()]
    positives = sum(part["positives"] for part in parts)
    caught = sum(part["true_positives"] for part in parts)
    if positives != candidate["tp"] + candidate["fn"]:
        raise BiasError(f"group plus excluded positives {positives} do not equal the candidate's "
                        f"tp + fn {candidate['tp'] + candidate['fn']}")
    if caught != candidate["tp"]:
        raise BiasError(f"group plus excluded true positives {caught} do not equal the candidate's tp {candidate['tp']}")


def bias_check(rows: pd.DataFrame, loaded: gates.Gates, mapping: dict[str, str], data_version: str) -> dict:
    """The fairness gate of ADR-026 on one model's validation predictions: pass, the gap, the best and
    worst groups, the pooled and excluded rows, and the counts for metrics.json.

    It calls gates.evaluate(), so the floor, pooling, limit and exact arithmetic are the gate's own. The
    accuracy gate is not this check's concern, so the baseline block is the candidate's own counts; only
    the fairness part of the report is read. A case the gate cannot evaluate raises BiasError."""
    rows = rows[rows["split"] == "validation"] if "split" in rows else rows
    if rows.empty:
        raise BiasError("no validation rows")
    candidate = confusion(rows)
    groups, excluded = gate_counts(rows, mapping, loaded.fairness_groups)
    check_reconciliation(groups, excluded, candidate)
    report = gates.evaluate(loaded, {"data_version": data_version, "candidate": candidate, "baseline": candidate,
                                     "champion": None, "groups": groups, "excluded": excluded})
    if report["decision"] == "error":
        raise BiasError(f"the fairness gate cannot be evaluated: {report['reason']}")
    fairness = report["fairness_gate"]
    return {"pass": bool(fairness["pass"]), "gap": fairness["gap"], "threshold": fairness["threshold"],
            "best_group": fairness["best_group"], "worst_group": fairness["worst_group"],
            "reason": fairness["reason"], "groups": report["groups"], "pooled": report["pooled"],
            "excluded": report["excluded"], "counts": {"candidate": candidate, "groups": groups, "excluded": excluded}}


# Reported slices


def merged(group: pd.Series, surge: pd.Series, floor: int) -> pd.Series:
    """Groups with fewer than floor surges become one merged group named after its members, as the gate
    pools them; ungrouped rows are dropped."""
    group = group.where(group != UNGROUPED)
    counts = surge.groupby(group).sum()
    small = sorted(counts[counts < floor].index)
    if len(small) > 1:
        group = group.replace({name: "merged: " + " + ".join(small) for name in small})
    return group


def _metrics(frame_rows: pd.DataFrame, group: pd.Series) -> pd.DataFrame:
    from fairlearn.metrics import MetricFrame, count
    from sklearn.metrics import f1_score, precision_score, recall_score

    keep = group.notna()
    y, p = frame_rows.loc[keep, "label"].astype(int), frame_rows.loc[keep, "prediction"].astype(int)
    if y.empty:
        return pd.DataFrame()
    frame = MetricFrame(
        metrics={"surges": lambda t, _: int(t.sum()), "true_positives": lambda t, q: int((t & q).sum()),
                 "days": count, "recall": lambda t, q: recall_score(t, q, zero_division=np.nan),
                 "precision": lambda t, q: precision_score(t, q, zero_division=np.nan),
                 "f1": lambda t, q: f1_score(t, q, zero_division=np.nan)},
        y_true=y.to_numpy(), y_pred=p.to_numpy(), sensitive_features=group[keep].rename("group").to_numpy())
    return frame.by_group.reset_index().rename(columns={"sensitive_feature_0": "group"})


def slice_table(rows: pd.DataFrame, groups: pd.DataFrame, floor: int, splits=SPLITS) -> pd.DataFrame:
    """Every family's per-group results per split, plus each neighborhood and the higher-poverty
    group in two parts; small groups merged within a family (merged)."""
    tables = []
    for split in splits:
        part = rows[rows["split"] == split]
        if part.empty:
            continue
        surge = part["label"].astype(int)
        families = {name: part["unit"].map(groups[name]) for name in FAMILIES}
        families[NEIGHBORHOOD] = part["unit"].where(part["unit"].map(groups["poverty"]) != UNGROUPED)
        higher = part["unit"].map(groups["poverty"]) == "higher"
        two_parts = part["unit"].map(groups["student_area"]).map(lambda g: "student areas" if g == STUDENT else "other")
        families[TWO_PARTS] = two_parts.where(higher)
        for family, group in families.items():
            group = group if family == NEIGHBORHOOD else merged(group, surge, floor)
            table = _metrics(part, group)
            if len(table):
                tables.append(table.assign(split=split, family=family))
    if not tables:
        return pd.DataFrame(columns=["split", "family", "group", "days", "surges", "true_positives",
                                     "recall", "precision", "f1"])
    out = pd.concat(tables, ignore_index=True)
    out[["days", "surges", "true_positives"]] = out[["days", "surges", "true_positives"]].astype(int)
    return out[["split", "family", "group", "days", "surges", "true_positives", "recall", "precision", "f1"]]


def triggers(table: pd.DataFrame, groups: pd.DataFrame, loaded: gates.Gates) -> list[dict]:
    """ADR-026's two "Reopen if" conditions on validation, with the numbers behind each one that holds."""
    floor, limit = loaded.fairness_min_group_surges, float(loaded.fairness_max_gap)
    validation = table[table["split"] == "validation"]
    poverty = validation[validation["family"] == "poverty"].set_index("group")["recall"]
    found = []
    for row in validation[validation["family"] == NEIGHBORHOOD].itertuples(index=False):
        own = groups.loc[row.group, "poverty"]
        if row.surges >= floor and own in poverty and row.recall < poverty[own] - limit:
            found.append({"condition": "neighborhood below its group", "neighborhood": row.group, "group": own,
                          "surges": int(row.surges), "recall": row.recall, "group_recall": poverty[own],
                          "below": poverty[own] - row.recall})
    parts = validation[validation["family"] == TWO_PARTS].set_index("group")["recall"]
    if len(parts) == 2 and parts.notna().all() and abs(parts.iloc[0] - parts.iloc[1]) > limit:
        found.append({"condition": "higher group's two parts differ", "recalls": parts.to_dict(),
                      "difference": abs(parts.iloc[0] - parts.iloc[1])})
    return found


def reporting_bias(features: pd.DataFrame, groups: pd.DataFrame, splits: dict, names=("train", *SPLITS)) -> list[dict]:
    """Storm requests per 1,000 residents a year by poverty group, from the raw 311 lags (the counts on
    D - 2, the same requests a day later) and the population: the reporting gap #32 found (ADR-025)."""
    missing = {"unit", "date", POPULATION, *LAGS} - set(features.columns)
    if missing:
        raise BiasError(f"features are missing columns: {sorted(missing)}")
    rows = features.assign(group=features["unit"].map(groups["poverty"]), requests=features[list(LAGS)].sum(axis=1))
    rows = rows[rows["group"].isin([g for g in groups["poverty"].unique() if g != UNGROUPED])
                & rows[POPULATION].notna() & rows["requests"].notna()]
    out = []
    for split in names:
        first, last = splits[split]
        part = rows[pd.to_datetime(rows["date"]).between(first, last)]
        for name, group in part.groupby("group"):
            out.append({"split": split, "group": name, "requests": float(group["requests"].sum()),
                        "per_1000_a_year": float(group["requests"].sum() / group[POPULATION].sum() * 1000 * YEAR_DAYS)})
    return out


# Report


def _value(value, digits: int = 3) -> str:
    return "" if value is None or (isinstance(value, float) and np.isnan(value)) else f"{value:.{digits}f}"


def _markdown(frame: pd.DataFrame) -> str:
    lines = ["| " + " | ".join(frame.columns) + " |", "|" + "---|" * len(frame.columns)]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join(lines)


def report(model: str, check: dict, table: pd.DataFrame, found: list[dict], reporting: list[dict], floor: int) -> str:
    lines = [f"# Model bias: {model} (#110, ADR-033)", "",
             f"**Fairness gate (ADR-026):** {'pass' if check['pass'] else 'FAIL'}. Worst-group validation recall "
             f"gap {check['gap']:.3f} ({check['best_group']} against {check['worst_group']}), limit {check['threshold']:g}. "
             f"Groups with fewer than {floor} surges are merged, as the gate pools them.", ""]
    shown = table.assign(recall=table["recall"].map(_value), precision=table["precision"].map(_value),
                         f1=table["f1"].map(_value))
    lines += ["## Gated: ACS poverty groups", "", _markdown(shown[shown["family"] == "poverty"]), ""]
    lines += ["## Diagnostic: the higher-poverty group in two parts (#126)", "",
              "Student areas have at least twice the city's share of college and graduate students (#32's rule). "
              "Reported, not gated.", "", _markdown(shown[shown["family"] == TWO_PARTS]), ""]
    lines += ["## ADR-026 reopen conditions (validation)", ""]
    if found:
        for item in found:
            if item["condition"] == "neighborhood below its group":
                lines.append(f"- **{item['neighborhood']}** ({item['group']} poverty): recall {item['recall']:.3f} on "
                             f"{item['surges']} surges, {item['below']:.3f} below its group's {item['group_recall']:.3f}.")
            else:
                lines.append(f"- **The higher group's two parts** differ by {item['difference']:.3f}: {item['recalls']}.")
        lines += ["", "A condition holding does not block promotion; ADR-026 asks its owner to reopen the gate design. "
                  "With so few surges per neighborhood these recalls are noisy.", ""]
    else:
        lines += ["None holds.", ""]
    lines += ["## Reported, not gated", "", _markdown(shown[shown["family"].isin(["poc", "limited_english", "flood_zone"])]), "",
              "## Each neighborhood", "", _markdown(shown[shown["family"] == NEIGHBORHOOD]), ""]
    per_capita = pd.DataFrame(reporting)
    lines += ["## Residual reporting bias (#32, ADR-025)", "",
              "311 counts reported demand, not need. The model's 311 inputs are self-relative (ADR-025), but the "
              "labels still come from reported requests, so a neighborhood that reports less has fewer chances to "
              "reach the surge floor. Storm requests per 1,000 residents a year, from the raw lags:", ""]
    if not len(per_capita):
        lines.append(f"Not available: features.parquet has no {POPULATION} or raw lags ({', '.join(LAGS)}).")
    else:
        wide = per_capita.pivot(index="split", columns="group", values="per_1000_a_year")
        wide = wide.reindex([name for name in ("train", *SPLITS) if name in wide.index])
        if {"higher", "lower"} <= set(wide.columns):
            wide["higher over lower"] = wide["higher"] / wide["lower"]
        lines.append(_markdown(wide.round(2).reset_index()))
    return "\n".join(lines) + "\n"


def plot(table: pd.DataFrame, limit: float, path: Path) -> None:
    from matplotlib.figure import Figure

    shown = table[(table["split"] == "validation") & table["family"].isin([*FAMILIES, TWO_PARTS])]
    figure = Figure(figsize=(9, 4.5))
    axes = figure.subplots()
    labels = [f"{f}: {g}" for f, g in zip(shown["family"], shown["group"])]
    colors = ["#1f6f8b" if f == "poverty" else "#9aa5ab" for f in shown["family"]]
    axes.barh(labels, shown["recall"], color=colors)
    poverty = shown[shown["family"] == "poverty"]["recall"]
    if len(poverty):
        axes.axvline(poverty.max() - limit, color="#b03a2e", linestyle="--",
                     label=f"best gated recall minus the limit ({limit:g})")
        axes.legend(loc="lower right", fontsize=8)
    axes.set_xlabel("validation recall")
    axes.set_xlim(0, 1)
    axes.invert_yaxis()
    axes.set_title("Validation recall by group (dark: gated)")
    figure.tight_layout()
    figure.savefig(path, dpi=120)


# Running it on a training run


def analyse(run_dir: str | Path, features_path: str | Path, splits: dict, out_dir: str | Path | None = None,
            loaded: gates.Gates | None = None, groups_path: str | Path = SLICE_GROUPS,
            report_splits=SPLITS, rows: pd.DataFrame | None = None) -> tuple[dict, list[Path]]:
    """Run the analysis on a train.py run folder and write its four files; returns bias.json's content.
    report_splits limits the per-group tables and the reporting-bias rows to those splits (with train for
    the reporting bias): the model workflow passes ("validation",), so no test number enters its step.
    rows, when given, are the predictions already read (the workflow's validation rows); otherwise
    predictions.parquet is read through the predictions.py contract."""
    import predictions

    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if rows is None:
        rows = predictions.read_predictions(run_dir / "predictions.parquet", run["threshold"])
    rows = rows[rows["split"].isin(report_splits)]  # in the workflow, validation only: no test row is kept
    loaded = loaded or gates.load_gates()
    groups = load_groups(loaded, groups_path)
    check = bias_check(rows, loaded, groups["poverty"].to_dict(), run["data_version"])
    table = slice_table(rows, groups, loaded.fairness_min_group_surges, report_splits)
    found = triggers(table, groups, loaded)
    import pyarrow.parquet as pq

    wanted = ["unit", "date", POPULATION, *LAGS]
    if set(wanted) <= set(pq.read_schema(features_path).names):
        features = pd.read_parquet(features_path, columns=wanted)
        reporting = reporting_bias(features, groups, splits, ("train", *report_splits))
    else:  # a features file without the raw lags or the population: the reporting bias cannot be measured
        reporting = []

    out = Path(out_dir or run_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = [out / name for name in ("bias.json", "bias_slices.csv", "bias_report.md", "bias_recall.png")]
    content = {"model": run["model"], "run": run["run_id"], "data_version": run["data_version"],
               "gate": {key: value for key, value in check.items() if key != "counts"}, "counts": check["counts"],
               "splits": list(report_splits), "triggers": found, "reporting_bias": reporting,
               "groups_files": {"gated": loaded.fairness_group_source, "reported": Path(groups_path).name}}
    files[0].write_text(json.dumps(content, indent=2, default=float) + "\n", encoding="utf-8")
    table.to_csv(files[1], index=False)
    files[2].write_text(report(run["model"], check, table, found, reporting, loaded.fairness_min_group_surges),
                        encoding="utf-8")
    plot(table, float(loaded.fairness_max_gap), files[3])
    return content, files


def log_files(run_dir: Path, files: list[Path], settings) -> None:
    """Log the files to the training run's MLflow run (run.json names it), with the gate's result as tags."""
    import mlflow

    import tracking

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    tracking.setup(settings.mlflow_experiment, settings.mlflow_store)
    with mlflow.start_run(run_id=run["mlflow"]["run_id"]):
        for path in files:
            mlflow.log_artifact(str(path), artifact_path="bias")
        content = json.loads(files[0].read_text(encoding="utf-8"))
        mlflow.log_metric("validation_worst_group_recall_gap", content["gate"]["gap"])
        mlflow.set_tags({"bias_gate": "pass" if content["gate"]["pass"] else "fail",
                         "bias_triggers": str(len(content["triggers"]))})


def main(argv: list[str] | None = None) -> int:
    import training_data

    parser = argparse.ArgumentParser(description="Per-group results and the fairness gate of a training run (#110).")
    parser.add_argument("--run", help="a run folder written by train.py")
    parser.add_argument("--out-dir", help="write the files here instead of the run folder")
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG), help="training.csv path")
    parser.add_argument("--no-mlflow", action="store_true", help="do not log the files to the run's MLflow run")
    args = parser.parse_args(argv)
    try:
        if not args.run:
            parser.error("--run is required")
        settings = training_data.load_settings(args.config)
        content, files = analyse(args.run, settings.features, settings.splits, args.out_dir)
        if not args.no_mlflow:
            log_files(Path(args.run), files, settings)
    except (BiasError, gates.ConfigError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gate = content["gate"]
    print(f"{content['model']}: fairness gate {'pass' if gate['pass'] else 'FAIL'}, gap {gate['gap']:.3f} "
          f"({gate['best_group']} against {gate['worst_group']}), limit {gate['threshold']:g}; "
          f"{len(content['triggers'])} ADR-026 reopen conditions hold; wrote {files[0].parent}")
    return 0 if gate["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
