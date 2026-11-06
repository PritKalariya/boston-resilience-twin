"""Tests for the shared threshold rule and the surge-class metrics (#106, ADR-026, ADR-027)."""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
import threshold as T  # noqa: E402


def test_the_rule_picks_the_threshold_with_the_highest_f1():
    # 0.9: tp 1 fp 0 fn 1, F1 2/3; 0.8: 1 1 1, 1/2; 0.7: 2 1 0, 4/5; 0.2: 2 2 0, 2/3
    assert T.shared_threshold([0.9, 0.8, 0.7, 0.2], [1, 0, 1, 0]) == 0.7


def test_a_tie_keeps_the_higher_threshold():
    # 0.9 and 0.6 both give F1 2/3; the higher one raises fewer alarms
    assert T.shared_threshold([0.9, 0.8, 0.7, 0.6], [1, 0, 0, 1]) == 0.9


def test_equal_probabilities_are_one_candidate():
    # at 0.5 both rows at 0.5 are predicted surges together: tp 1 fp 1, F1 2/3; 0.1 gives 1/2
    assert T.shared_threshold([0.5, 0.5, 0.1], [1, 0, 0]) == 0.5


def _brute_force(p, y):
    best, best_t = Fraction(-1), None
    positives = int(sum(y))
    for t in sorted(set(p), reverse=True):
        tp = sum(1 for pi, yi in zip(p, y) if pi >= t and yi == 1)
        fp = sum(1 for pi, yi in zip(p, y) if pi >= t and yi == 0)
        f1 = Fraction(2 * tp, 2 * tp + fp + (positives - tp))
        if f1 > best:
            best, best_t = f1, t
    return best_t


@pytest.mark.parametrize("seed", range(20))
def test_the_rule_matches_an_exhaustive_search(seed):
    rng = np.random.default_rng(seed)
    p = np.round(rng.random(60), 1)  # many ties
    y = (rng.random(60) < 0.3).astype(int)
    y[0] = 1
    assert T.shared_threshold(p, y) == _brute_force(p.tolist(), y.tolist())


@pytest.mark.parametrize("probability, label, message", [
    ([0.3, 0.2], [0, 0], "no surge in these labels"),
    ([], [], "no rows to score"),
    ([0.3, np.nan], [1, 0], "1 probabilities are missing"),
    ([0.3, 1.2], [1, 0], "from 0 to 1"),
    ([0.3, 0.2], [1, 2], "labels must be 0 or 1"),
    ([0.3, 0.2, 0.1], [1, 0], "same length"),
])
def test_bad_input_is_refused(probability, label, message):
    with pytest.raises(ValueError, match=message):
        T.shared_threshold(probability, label)


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError, match="unknown threshold rule"):
        T.shared_threshold([0.3], [1], rule="fixed_half")


def test_auc_from_ranks():
    assert T.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert T.auc([0.5, 0.5], [0, 1]) == 0.5  # a tie counts half
    assert T.auc([0.2, 0.4], [1, 1]) is None  # one class only


def test_metrics_report_counts_and_ratios_at_the_threshold():
    m = T.metrics([0.9, 0.8, 0.7, 0.2], [1, 0, 1, 0], 0.7)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 0, 1)
    assert (m["rows"], m["surges"], m["threshold"]) == (4, 2, 0.7)
    assert m["f1"] == pytest.approx(0.8) and m["precision"] == pytest.approx(2 / 3) and m["recall"] == 1.0
    assert m["auc"] == 0.75


def test_a_ratio_with_a_zero_denominator_is_none_not_zero():
    nothing_predicted = T.metrics([0.1, 0.2], [1, 0], 0.5)
    assert nothing_predicted["precision"] is None and nothing_predicted["recall"] == 0.0
    no_surge = T.metrics([0.1, 0.2], [0, 0], 0.5)
    assert no_surge["recall"] is None and no_surge["f1"] is None and no_surge["auc"] is None


def test_counts_match_the_prediction_rule():
    c = T.counts([0.5, 0.49, 0.5], [1, 1, 0], 0.5)
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 0)
