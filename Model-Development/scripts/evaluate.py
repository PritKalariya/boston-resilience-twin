"""Evaluate a training run (#106, ADR-027).

    python Model-Development/scripts/evaluate.py --run Model-Development/runs/<run id> [--gates-block]

Reads the run's predictions.parquet through the predictions.py contract (so a broken file fails here)
and its threshold from run.json, recomputes F1, precision, recall, AUC and the counts per split, and
fails if they differ from the run's metrics.json. --gates-block prints only the run's validation tp,
fp and fn, the candidate or baseline block of the promotion gates input (ADR-026).

Exits 0 when the run checks out and 2, with the reason, otherwise.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import predictions
import threshold


def evaluate(run_dir: str | Path) -> dict:
    """The metrics per split recomputed from the run's predictions; fails if metrics.json disagrees."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cut = run["threshold"]
    frame = predictions.read_predictions(run_dir / "predictions.parquet", cut)
    if set(frame["model"]) != {run["model"]}:
        raise ValueError(f"predictions.parquet holds models {sorted(set(frame['model']))}, run.json names {run['model']!r}")
    scores = {split: threshold.metrics(part["probability"], part["label"], cut)
              for split, part in frame.groupby("split", sort=False)}

    stored = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    if set(stored) != set(scores):
        raise ValueError(f"metrics.json has splits {sorted(stored)}, the predictions have {sorted(scores)}")
    for split, mine in scores.items():
        for key, value in mine.items():
            theirs = stored[split].get(key)
            same = (value is None and theirs is None) or (
                value is not None and theirs is not None and math.isclose(value, theirs, rel_tol=1e-12, abs_tol=0))
            if not same:
                raise ValueError(f"{split} {key}: metrics.json has {theirs}, the predictions give {value}")
    return scores


def gates_block(scores: dict) -> dict:
    """The validation tp, fp and fn in the shape the gates input takes for a candidate or the baseline."""
    validation = scores["validation"]
    return {"tp": validation["tp"], "fp": validation["fp"], "fn": validation["fn"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a training run and print its metrics (#106).")
    parser.add_argument("--run", required=True, help="a run folder written by train.py")
    parser.add_argument("--gates-block", action="store_true", help="print only the validation tp, fp and fn as JSON")
    args = parser.parse_args(argv)
    try:
        scores = evaluate(args.run)
    except Exception as exc:  # a missing file, a broken contract or metrics that disagree
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.gates_block:
        print(json.dumps(gates_block(scores)))
    else:
        print(json.dumps(scores, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
