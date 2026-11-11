"""Test results by kind of day (#107): a model against the baseline over the whole test window and
on storm, freeze and heat days, with a bootstrap interval for the F1 gain.

The kinds of day are the rules in Model-Development/config/slices.csv (slices in training.csv). A
slice holds the rows where any of its rules holds; a rule compares one input of features.parquet
with a number, and a missing value holds no rule. The inputs are the ones known at the evening
cutoff (forecast weather, alerts known by 18:00), so a slice is a kind of day as the model saw it
the evening before. Weather and alerts are city-wide, so a slice holds whole days.

Per slice, for the model and the baseline at their own run thresholds: the days, rows and surges,
tp, fp, fn and tn, and F1, precision and recall. A value that cannot be computed is None, never 0:
a slice with no surge has no recall, and with no surge and no alarm no F1 either.

The F1 gain (model minus baseline) gets a 95% interval from a bootstrap that resamples whole days,
since the neighborhoods of one day share its weather and are not independent draws (a cluster
bootstrap; Efron and Tibshirani 1993): bootstrap_resamples draws of the slice's days with
replacement (seed bootstrap_seed), and the 2.5th and 97.5th percentiles of the gains of the draws
where both F1s exist. With fewer than 95% of the draws usable, the interval is None.
"""

from __future__ import annotations

import csv
import math
import operator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

HEADER = ["slice", "input", "operator", "value", "note"]
OPERATORS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
ALL = "all"  # every test row; the name no rule may take
KEYS = ["unit", "date"]
MIN_USABLE = 0.95  # the share of bootstrap draws with both F1s an interval needs


@dataclass(frozen=True)
class Rule:
    slice: str
    input: str
    operator: str
    value: float


def load_rules(path: str | Path) -> dict[str, list[Rule]]:
    """The rules of slices.csv by slice, in file order; fails naming the row on a bad header,
    operator or value, or a slice called all."""
    path = Path(path)
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None or [c.strip() for c in header] != HEADER:
            raise ValueError(f"{path.name} needs the header {','.join(HEADER)}")
        rules: dict[str, list[Rule]] = {}
        for number, row in enumerate(reader, start=2):
            if not row or not row[0].strip():
                continue
            name, column, op, value = (cell.strip() for cell in row[:4])
            if name == ALL:
                raise ValueError(f"{path.name} row {number}: {ALL!r} is every test row, not a slice to define")
            if not column:
                raise ValueError(f"{path.name} row {number}: the slice {name} needs an input")
            if op not in OPERATORS:
                raise ValueError(f"{path.name} row {number}: operator {op!r} is not one of {list(OPERATORS)}")
            try:
                number_value = float(value)
            except ValueError:
                number_value = math.nan
            if not math.isfinite(number_value):
                raise ValueError(f"{path.name} row {number}: value {value!r} is not a number")
            rules.setdefault(name, []).append(Rule(name, column, op, number_value))
    if not rules:
        raise ValueError(f"{path.name} defines no slice")
    return rules


def check_inputs(rules: dict[str, list[Rule]], columns) -> None:
    """Fails naming the first slice with a rule on an input that is not among columns."""
    for name, group in rules.items():
        for rule in group:
            if rule.input not in columns:
                raise ValueError(f"slice {name}: the input {rule.input} is not in the features")


def masks(rows: pd.DataFrame, rules: dict[str, list[Rule]]) -> dict[str, np.ndarray]:
    """Which rows each slice holds: all of them for all, else those where any rule holds."""
    check_inputs(rules, rows.columns)
    out = {ALL: np.ones(len(rows), dtype=bool)}
    for name, group in rules.items():
        mask = np.zeros(len(rows), dtype=bool)
        for rule in group:
            with np.errstate(invalid="ignore"):  # a missing value compares False: it holds no rule
                mask |= OPERATORS[rule.operator](rows[rule.input].to_numpy(dtype="float64"), rule.value)
        out[name] = mask
    return out


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _metrics(label: np.ndarray, prediction: np.ndarray) -> dict:
    tp = int(((prediction == 1) & (label == 1)).sum())
    fp = int(((prediction == 1) & (label == 0)).sum())
    fn = int(((prediction == 0) & (label == 1)).sum())
    tn = int(((prediction == 0) & (label == 0)).sum())
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "f1": _ratio(2 * tp, 2 * tp + fp + fn),
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn)}


def _f1s(counts: np.ndarray) -> np.ndarray:
    """F1 of each row of (tp, fp, fn) counts; NaN where undefined."""
    numerator = 2 * counts[:, 0]
    denominator = numerator + counts[:, 1] + counts[:, 2]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(denominator > 0, numerator / denominator, np.nan)


def bootstrap_gain(days, model: np.ndarray, baseline: np.ndarray, resamples: int,
                   seed: int) -> tuple[float | None, float | None, float]:
    """The 95% interval of the F1 gain (model minus baseline) over draws of whole days, and the
    share of draws where both F1s exist. days names each row's day; model and baseline hold each
    row's (tp, fp, fn) as 0 or 1."""
    _, day = np.unique(np.asarray(days), return_inverse=True)
    if not len(day):
        return None, None, 0.0
    per_day = np.zeros((day.max() + 1, 2, 3))
    np.add.at(per_day, day, np.stack([model, baseline], axis=1))
    draws = np.random.default_rng(seed).integers(0, len(per_day), size=(resamples, len(per_day)))
    totals = per_day[draws].sum(axis=1)  # resamples x 2 x 3
    gains = _f1s(totals[:, 0]) - _f1s(totals[:, 1])
    usable = ~np.isnan(gains)
    share = float(usable.mean())
    if share < MIN_USABLE:
        return None, None, share
    low, high = np.percentile(gains[usable], [2.5, 97.5])
    return float(low), float(high), share


def _indicators(label: np.ndarray, prediction: np.ndarray) -> np.ndarray:
    """Each row's (tp, fp, fn) as 0 or 1."""
    return np.stack([(prediction == 1) & (label == 1), (prediction == 1) & (label == 0),
                     (prediction == 0) & (label == 1)], axis=1).astype("float64")


def slice_table(model_rows: pd.DataFrame, baseline_rows: pd.DataFrame, features: pd.DataFrame,
                rules: dict[str, list[Rule]], resamples: int, seed: int) -> list[dict]:
    """One record per slice (all first): the model and the baseline on the same test rows, each
    given as unit, date, label and prediction (a run's predictions.parquet), with the features for
    the rules."""
    joined = model_rows[[*KEYS, "label", "prediction"]].merge(
        baseline_rows[[*KEYS, "label", "prediction"]], on=KEYS, suffixes=("_model", "_baseline"), how="outer",
        indicator=True)
    if (joined["_merge"] != "both").any() or (joined["label_model"] != joined["label_baseline"]).any():
        raise ValueError("the model and the baseline do not hold the same test rows and labels")
    joined = joined.drop(columns="_merge").merge(features, on=KEYS, how="left", validate="one_to_one")
    out = []
    for name, mask in masks(joined, rules).items():
        part = joined[mask]
        label = part["label_model"].to_numpy()
        record = {"slice": name, "days": int(part["date"].nunique()), "rows": len(part), "surges": int(label.sum())}
        for who in ("model", "baseline"):
            record[who] = _metrics(label, part[f"prediction_{who}"].to_numpy())
        f1_model, f1_baseline = record["model"]["f1"], record["baseline"]["f1"]
        record["f1_gain"] = None if f1_model is None or f1_baseline is None else f1_model - f1_baseline
        low, high, share = bootstrap_gain(part["date"].to_numpy(), _indicators(label, part["prediction_model"].to_numpy()),
                                          _indicators(label, part["prediction_baseline"].to_numpy()), resamples, seed)
        record["f1_gain_ci95"] = None if low is None else [low, high]
        record["bootstrap_draws_usable"] = share
        out.append(record)
    return out


def flat(records: list[dict]) -> list[dict]:
    """slice_table's records as CSV rows: the model's and the baseline's numbers side by side."""
    rows = []
    for record in records:
        row = {key: record[key] for key in ("slice", "days", "rows", "surges")}
        for who in ("model", "baseline"):
            row.update({f"{who}_{name}": value for name, value in record[who].items()})
        interval = record["f1_gain_ci95"] or [None, None]
        row.update({"f1_gain": record["f1_gain"], "f1_gain_ci95_low": interval[0], "f1_gain_ci95_high": interval[1],
                    "bootstrap_draws_usable": record["bootstrap_draws_usable"]})
        rows.append(row)
    return rows
