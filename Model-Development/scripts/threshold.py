"""The shared threshold rule and the surge-class metrics (#106, ADR-026, ADR-027).

ADR-026 compares every candidate with the seasonal baseline after both are "thresholded by the same
shared rule on the validation split". The rule (threshold_rule = validation_f1_max in
Model-Development/config/training.csv):

- A day is predicted a surge when its probability is at or above the threshold.
- The threshold is the probability value that gives the highest surge-class F1 on the validation
  split (2024), F1 = 2tp / (2tp + fp + fn). On a tie the higher threshold wins, so fewer alarms.
- The run saves the threshold and applies it unchanged to the test split, which never picks it.

F1 values are compared exactly, as integer ratios, so a tie is a real tie (ADR-026 compares the gates
on exact fractions for the same reason). A validation split with no surge has no F1, so no threshold
can be chosen and the rule fails instead of guessing (ADR-026: on_undefined = fail).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

RULES = ("validation_f1_max",)


@dataclass(frozen=True)
class Counts:
    tp: int
    fp: int
    fn: int
    tn: int


def _checked(probability, label) -> tuple[np.ndarray, np.ndarray]:
    """probability and label as arrays; fails on a length mismatch, no rows, a probability that is
    missing or outside 0 to 1, or a label that is not 0 or 1."""
    p = np.asarray(probability, dtype="float64")
    y = np.asarray(label)
    if p.ndim != 1 or y.ndim != 1 or len(p) != len(y):
        raise ValueError(f"probability and label must be two lists of the same length, got {p.shape} and {y.shape}")
    if len(p) == 0:
        raise ValueError("no rows to score")
    if np.isnan(p).any():
        raise ValueError(f"{int(np.isnan(p).sum())} probabilities are missing")
    if ((p < 0) | (p > 1)).any():
        raise ValueError("probabilities must be from 0 to 1")
    if not np.isin(y, [0, 1]).all():
        raise ValueError("labels must be 0 or 1")
    return p, y.astype("int64")


def counts(probability, label, threshold: float) -> Counts:
    """tp, fp, fn and tn with a surge predicted at probability >= threshold."""
    p, y = _checked(probability, label)
    predicted = p >= threshold
    return Counts(tp=int((predicted & (y == 1)).sum()), fp=int((predicted & (y == 0)).sum()),
                  fn=int((~predicted & (y == 1)).sum()), tn=int((~predicted & (y == 0)).sum()))


def shared_threshold(probability, label, rule: str = "validation_f1_max") -> float:
    """The threshold the shared rule picks on these (validation) rows; see the module docstring."""
    if rule not in RULES:
        raise ValueError(f"unknown threshold rule {rule!r}; known rules: {list(RULES)}")
    p, y = _checked(probability, label)
    positives = int(y.sum())
    if positives == 0:
        raise ValueError("no surge in these labels: F1 is undefined, so no threshold can be chosen")
    order = np.argsort(-p, kind="stable")
    p_sorted, y_sorted = p[order], y[order]
    tp_at = np.cumsum(y_sorted)
    fp_at = np.cumsum(1 - y_sorted)
    # Each distinct probability, from the highest down, is a candidate threshold; at its last position
    # in the sorted order every row with a probability at or above it is predicted a surge.
    last = np.append(np.nonzero(np.diff(p_sorted))[0], len(p_sorted) - 1)
    best_num, best_den, best_t = -1, 1, float("nan")
    for i in last:
        tp, fp = int(tp_at[i]), int(fp_at[i])
        num, den = 2 * tp, 2 * tp + fp + (positives - tp)
        if num * best_den > best_num * den:  # strictly better; a tie keeps the higher threshold seen first
            best_num, best_den, best_t = num, den, float(p_sorted[i])
    return best_t


def auc(probability, label) -> float | None:
    """Area under the ROC curve from ranks (ties share their average rank); None with one class only."""
    p, y = _checked(probability, label)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = pd.Series(p).rank(method="average").to_numpy()
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def metrics(probability, label, threshold: float) -> dict:
    """Surge-class F1, precision and recall at threshold, AUC, and the counts they come from.
    A metric with a zero denominator is None (no prediction or no surge), never 0."""
    c = counts(probability, label, threshold)
    return {
        "rows": c.tp + c.fp + c.fn + c.tn,
        "surges": c.tp + c.fn,
        "threshold": float(threshold),
        "tp": c.tp, "fp": c.fp, "fn": c.fn, "tn": c.tn,
        "f1": _ratio(2 * c.tp, 2 * c.tp + c.fp + c.fn),
        "precision": _ratio(c.tp, c.tp + c.fp),
        "recall": _ratio(c.tp, c.tp + c.fn),
        "auc": auc(probability, label),
    }
