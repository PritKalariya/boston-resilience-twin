"""Model selection (#107): train the baseline and every candidate on one data version, rank the
candidates by validation F1, and keep the best only if it beats the baseline by the margin of the
ADR-026 accuracy gate. Named selection.py, never select.py, which would hide Python's own select
module from every script in this folder.

    python Model-Development/scripts/selection.py [--config Model-Development/config/training.csv] [--id <id>]

1. Trains the f1_baseline of gates.csv (seasonal) and each model in candidates (training.csv) with
   train.train, each logged to MLflow as a child of one selection run.
2. Ranks the candidates by validation F1, compared exactly as the fraction 2tp / (2tp + fp + fn), as
   gates.py does. On an exact tie the lower validation log loss wins, then the name. A candidate
   whose F1 cannot be computed (no surge and no alarm) ranks last and is never counted as 0.
3. The winner is the top candidate if its F1 is at least the baseline's plus f1_min_margin
   (gates.csv: 0.05, inclusive; ADR-026); otherwise there is none.
4. Writes to reports_dir (training.csv) and logs to the selection run:
   - selection.json: the decision, the rule and every model's numbers, run folder and MLflow ids;
   - comparison.csv and comparison.png: validation F1 against the gate, and beside it each model's
     training time, time to score one evening's rows and saved model size;
   - confusion_matrix.png: the winner's validation counts at its threshold (with no winner, the
     best candidate's, marked as such).

Exits 0 with a winner, 1 when no candidate beats the baseline by the margin, and 2 on any error,
as gates.py does.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

import tracking
import train
import training_data

GATES = training_data.ROOT / "Model-Development" / "config" / "gates.csv"
SHOWN = ("f1", "precision", "recall", "auc", "pr_auc", "log_loss", "brier_skill", "tp", "fp", "fn", "tn",
         "rows", "surges")  # the metrics selection.json repeats per split

# The charts use the dataviz reference palette on its light surface. Emphasis: the winner in blue,
# every other model gray (checked: normal-vision Delta E 17.8, protan 15.9, both above 3:1).
SURFACE, INK, INK_2, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
SELECTED, OTHER = "#2a78d6", "#898781"
RAMP = ("#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#184f95")  # sequential blue, light to dark
FONT = 9


def gate_rule(path: Path = GATES) -> tuple[str, Fraction]:
    """The accuracy gate (ADR-026) from gates.csv: the baseline model's name (f1_baseline) and the
    margin a candidate's validation F1 must clear above the baseline's (f1_min_margin, inclusive).
    Read alone, not through gates.load_gates: that also checks poverty_groups.csv's sha256, which a
    Windows checkout fails on its line endings; the gates themselves run in CI on LF files."""
    with open(path, encoding="utf-8", newline="") as handle:
        values = {row["key"].strip(): row["value"].strip() for row in csv.DictReader(handle)}
    try:
        margin = Fraction(values["f1_min_margin"])
    except (KeyError, ValueError):
        raise ValueError(f"{path.name} needs f1_min_margin as a number") from None
    if not 0 <= margin <= 1:
        raise ValueError(f"{path.name}: f1_min_margin must be from 0 to 1, got {values['f1_min_margin']!r}")
    if not values.get("f1_baseline"):
        raise ValueError(f"{path.name} needs f1_baseline, the baseline model's name")
    return values["f1_baseline"], margin


def exact_f1(counts: dict) -> Fraction | None:
    """2tp / (2tp + fp + fn) as an exact fraction; None when no surge and no alarm leave it undefined."""
    denominator = 2 * counts["tp"] + counts["fp"] + counts["fn"]
    return Fraction(2 * counts["tp"], denominator) if denominator else None


@dataclass
class Entry:
    """One trained model of a selection: its name, run folder, run.json and metrics.json."""
    model: str
    run_dir: Path
    run: dict
    scores: dict

    @property
    def f1(self) -> Fraction | None:
        return exact_f1(self.scores["validation"])

    @classmethod
    def read(cls, model: str, run_dir: Path) -> Entry:
        def load(name: str) -> dict:
            return json.loads((run_dir / name).read_text(encoding="utf-8"))
        return cls(model, run_dir, load("run.json"), load("metrics.json"))


def rank(candidates: list[Entry]) -> list[Entry]:
    """Best first: the higher exact validation F1; on a tie the lower validation log loss, then the
    name. A candidate with no F1 goes last."""
    return sorted(candidates, key=lambda e: (e.f1 is None, -(e.f1 or 0), e.scores["validation"]["log_loss"], e.model))


def decide(ranked: list[Entry], baseline: Entry, margin: Fraction) -> tuple[Fraction, Entry | None]:
    """The gate's bar (the baseline's validation F1 plus margin) and the winner: the top candidate if
    it reaches the bar, else None. Fails when the baseline's F1 is undefined (ADR-026: on_undefined = fail)."""
    if baseline.f1 is None:
        raise ValueError(f"the baseline {baseline.model} has no validation F1 (no surge and no alarm), so the "
                         "gate cannot be evaluated (ADR-026: on_undefined = fail)")
    bar = baseline.f1 + margin
    top = ranked[0] if ranked else None
    return bar, top if top is not None and top.f1 is not None and top.f1 >= bar else None


def _f(value) -> float | None:
    return None if value is None else round(float(value), 6)


def _row(entry: Entry, bar: Fraction | None) -> dict:
    """What selection.json says about one model."""
    run = entry.run
    return {"model": entry.model, "validation_f1": _f(entry.f1),
            "validation_f1_exact": None if entry.f1 is None else str(entry.f1),
            "reaches_gate": None if bar is None else (entry.f1 is not None and entry.f1 >= bar),
            "threshold": run["threshold"], "chosen": run["model_details"].get("chosen", {}),
            **{split: {name: entry.scores[split][name] for name in SHOWN} for split in ("validation", "test")},
            "costs": run["costs"], "run_folder": run["run_id"], "mlflow_run_id": run["mlflow"]["run_id"],
            "model_uri": run["mlflow"]["model_uri"]}


def report(selection_id: str, ranked: list[Entry], baseline: Entry, margin: Fraction, bar: Fraction,
           winner: Entry | None) -> dict:
    """selection.json: the decision first, then the rule and the numbers behind it."""
    versions = {e.run["data_version"] for e in (*ranked, baseline)}
    if len(versions) != 1:
        raise ValueError(f"the models were trained on different data versions: {sorted(versions)}")
    best = ranked[0] if ranked else None
    if winner is not None:
        decision = (f"{winner.model} wins: validation F1 {float(winner.f1):.4f} reaches the gate "
                    f"{float(bar):.4f} (baseline {float(baseline.f1):.4f} + {float(margin):.2f})")
    elif best is not None and best.f1 is not None:
        decision = (f"no winner: the best candidate, {best.model}, has validation F1 {float(best.f1):.4f}, "
                    f"under the gate {float(bar):.4f} (baseline {float(baseline.f1):.4f} + {float(margin):.2f})")
    else:
        decision = "no winner: no candidate has a validation F1"
    return {"selection_id": selection_id, "winner": None if winner is None else winner.model, "decision": decision,
            "rule": {"rank_by": "validation F1, exact; ties by validation log loss, then name",
                     "baseline": baseline.model, "f1_min_margin": str(margin), "source": "gates.csv (ADR-026)"},
            "gate_f1": _f(bar), "gate_f1_exact": str(bar), "data_version": versions.pop(),
            "git": baseline.run["git"], "baseline": _row(baseline, None), "ranking": [_row(e, bar) for e in ranked]}


def comparison_rows(ranked: list[Entry], baseline: Entry, winner: Entry | None, bar: Fraction) -> list[dict]:
    """The table behind comparison.png, one row per model: the ranked candidates, then the baseline."""
    rows = []
    for entry in (*ranked, baseline):
        costs, validation = entry.run["costs"], entry.scores["validation"]
        role = "baseline" if entry is baseline else "selected" if entry is winner else "candidate"
        rows.append({"model": entry.model, "role": role, "validation_f1": _f(entry.f1),
                     "reaches_gate": "" if entry is baseline else entry.f1 is not None and entry.f1 >= bar,
                     "precision": validation["precision"], "recall": validation["recall"],
                     "pr_auc": validation["pr_auc"], "train_time_s": costs["train_time_s"],
                     "score_time_evening_ms_median": costs["score_time_evening_ms"]["median"],
                     "score_time_evening_ms_p95": costs["score_time_evening_ms"]["p95"],
                     "evening_rows": costs["score_time_evening_ms"]["rows"],
                     "model_size_bytes": costs["model_size_bytes"]})
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({k: ("" if v is None else v) for k, v in row.items()} for row in rows)


LABEL_BOX = {"boxstyle": "square,pad=0.15", "facecolor": SURFACE, "edgecolor": "none"}  # lines pass behind text


def _panel(ax, labels, values, colors, title, text) -> None:
    """One horizontal bar panel: a bar per model with its value at the tip, n/a where none."""
    positions = range(len(labels))
    top = max((v for v in values if v is not None), default=1) or 1
    ax.barh(positions, [v or 0 for v in values], height=0.55, color=colors)
    for y, value in zip(positions, values):
        ax.text((value or 0) + top * 0.02, y, "n/a" if value is None else text(value), va="center",
                fontsize=FONT, color=INK_2, bbox=LABEL_BOX, zorder=3)
    ax.set_yticks(list(positions), labels, fontsize=FONT, color=INK)
    ax.set_xlim(0, top * 1.3)
    ax.set_title(title, fontsize=FONT + 1, color=INK, loc="left")
    ax.set_facecolor(SURFACE)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", colors=INK_2, labelsize=FONT - 1)
    ax.tick_params(axis="y", length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)


def write_comparison(rows: list[dict], bar: Fraction, margin: Fraction, path: Path) -> None:
    """comparison.png: validation F1 with the gate line, and beside it training time, scoring time
    for one evening's rows and saved model size; small multiples on one model order, each its own axis."""
    labels = [f"{r['model']} (baseline)" if r["role"] == "baseline" else r["model"] for r in rows]
    colors = [SELECTED if r["role"] == "selected" else OTHER for r in rows]
    figure = Figure(figsize=(13, 0.5 * len(rows) + 1.8), facecolor=SURFACE, layout="constrained")
    axes = figure.subplots(1, 4, sharey=True)
    _panel(axes[0], labels, [r["validation_f1"] for r in rows], colors, "Validation F1 (2024)", lambda v: f"{v:.4f}")
    axes[0].axvline(float(bar), color=INK, linewidth=1, linestyle=(0, (4, 3)), zorder=2)  # dashed: a threshold
    axes[0].text(float(bar), -0.8, f" gate {float(bar):.4f} = baseline + {float(margin):.2f}", fontsize=FONT - 1,
                 color=INK, va="center", ha="left")
    _panel(axes[1], labels, [r["train_time_s"] for r in rows], colors, "Training time (s)", lambda v: f"{v:.1f}")
    _panel(axes[2], labels, [r["score_time_evening_ms_median"] for r in rows], colors,
           f"Scoring {rows[0]['evening_rows']} rows (ms)", lambda v: f"{v:.1f}")
    _panel(axes[3], labels, [None if r["model_size_bytes"] is None else r["model_size_bytes"] / 1024 for r in rows],
           colors, "Saved model (KB)", lambda v: f"{v:,.0f}")
    axes[0].set_ylim(len(rows) - 0.5, -1.1)  # the shared axis once: best model on top, room above it for the gate
    figure.suptitle("Model selection: the selected model in blue, every other model gray. Scoring: the median "
                    "time for one evening's rows, one per neighborhood", fontsize=FONT + 1, color=INK, x=0.01, ha="left")
    figure.savefig(path, dpi=150)


def write_confusion(entry: Entry, selected: bool, path: Path) -> None:
    """confusion_matrix.png: the validation counts at the model's threshold, each shaded by its share of
    its row (what happened), with the count and the share written in."""
    v = entry.scores["validation"]
    cells = [[("surge, alarm", v["tp"]), ("surge, no alarm", v["fn"])],
             [("no surge, alarm", v["fp"]), ("no surge, no alarm", v["tn"])]]
    figure = Figure(figsize=(6.4, 4.6), facecolor=SURFACE, layout="constrained")
    ax = figure.subplots()
    for row, pair in enumerate(cells):
        total = sum(count for _, count in pair)
        for column, (name, count) in enumerate(pair):
            share = count / total if total else 0.0
            step = min(int(share * len(RAMP)), len(RAMP) - 1)
            ax.add_patch(Rectangle((column, row), 0.98, 0.98, color=RAMP[step]))
            ink = "#ffffff" if step >= 3 else INK
            ax.text(column + 0.49, row + 0.42, f"{count:,}", ha="center", va="center", fontsize=FONT + 6,
                    color=ink, fontweight="bold")
            ax.text(column + 0.49, row + 0.66, f"{name}\n{share:.1%} of the row", ha="center", va="center",
                    fontsize=FONT - 1, color=ink)
    ax.set_xlim(0, 2)
    ax.set_ylim(2, 0)
    ax.set_xticks([0.49, 1.49], ["alarm (predicted surge)", "no alarm"], fontsize=FONT, color=INK)
    ax.set_yticks([0.49, 1.49], ["surge day", "no surge"], fontsize=FONT, color=INK)
    ax.tick_params(length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    status = "selected model" if selected else "best candidate, not selected"
    ax.set_title(f"{entry.model} ({status}): validation 2024 at threshold {entry.run['threshold']:.4f}",
                 fontsize=FONT + 1, color=INK, loc="left")
    figure.savefig(path, dpi=150)


def run_selection(settings: training_data.Settings, selection_id: str | None = None,
                  reports_dir: str | Path | None = None, progress=print) -> tuple[dict, Path]:
    """Train, rank and decide (see the module docstring); returns selection.json's content and the reports folder."""
    baseline_name, margin = gate_rule()
    selection_id = selection_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-selection"
    out = Path(reports_dir or settings.reports_dir)
    tags = {"purpose": "model selection (#107)", "data_version": training_data.data_version(settings.dvc_lock)}
    with tracking.tracked_run(settings, selection_id, tags):
        entries = {}
        for name in (baseline_name, *settings.candidates):
            entries[name] = Entry.read(name, train.train(name, settings, f"{selection_id}-{name}"))
            f1 = entries[name].f1
            progress(f"trained {name}: validation F1 {'n/a' if f1 is None else f'{float(f1):.4f}'}", flush=True)
        baseline = entries[baseline_name]
        ranked = rank([entries[name] for name in settings.candidates])
        bar, winner = decide(ranked, baseline, margin)
        content = report(selection_id, ranked, baseline, margin, bar, winner)

        out.mkdir(parents=True, exist_ok=True)
        files = [out / name for name in ("selection.json", "comparison.csv", "comparison.png", "confusion_matrix.png")]
        files[0].write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        rows = comparison_rows(ranked, baseline, winner, bar)
        write_csv(rows, files[1])
        write_comparison(rows, bar, margin, files[2])
        write_confusion(winner or ranked[0], winner is not None, files[3])
        metrics = {"gate_f1": float(bar), "baseline_validation_f1": float(baseline.f1)}
        if winner is not None:
            metrics["winner_validation_f1"] = float(winner.f1)
        tracking.log_summary(params={"baseline": baseline_name, "candidates": list(settings.candidates),
                                     "f1_min_margin": str(margin)},
                             metrics=metrics, tags={"winner": content["winner"] or "none"}, files=files)
    return content, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the baseline and the candidates and select a model (#107).")
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG), help="training.csv path")
    parser.add_argument("--id", help="the selection's name, also the prefix of its run folders (default: UTC time)")
    parser.add_argument("--reports-dir", help="write the reports here instead of the config's reports_dir")
    args = parser.parse_args(argv)
    try:
        settings = training_data.load_settings(args.config)
        content, out = run_selection(settings, args.id, args.reports_dir)
    except Exception as exc:  # a bad config, missing data, a failed run or an undefined gate
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"selection {content['selection_id']}, data {content['data_version'][:19]}")
    print(f"{'model':<20}{'val F1':>8}{'gate':>7}{'precision':>11}{'recall':>8}{'pr_auc':>8}"
          f"{'train s':>9}{'score ms':>10}{'size KB':>9}")
    def cell(value, width: int) -> str:
        return f"{'n/a':>{width}}" if value is None else f"{value:>{width}.4f}"

    for row in (*content["ranking"], content["baseline"]):
        name = row["model"] + (" (baseline)" if row is content["baseline"] else "")
        gate = "" if row["reaches_gate"] is None else ("yes" if row["reaches_gate"] else "no")
        size = row["costs"]["model_size_bytes"]
        print(f"{name:<20}{cell(row['validation_f1'], 8)}{gate:>7}{cell(row['validation']['precision'], 11)}"
              f"{cell(row['validation']['recall'], 8)}{cell(row['validation']['pr_auc'], 8)}"
              f"{row['costs']['train_time_s']:>9.1f}{row['costs']['score_time_evening_ms']['median']:>10.1f}"
              f"{'n/a' if size is None else f'{size / 1024:,.0f}':>9}")
    print(content["decision"])
    print(f"wrote {out}")
    return 0 if content["winner"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
