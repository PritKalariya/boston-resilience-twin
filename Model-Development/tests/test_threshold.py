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
    assert list(m) == ["rows", "surges", "surge_rate", "threshold", "tp", "fp", "fn", "tn", "f1", "precision",
                       "recall", "auc", "pr_auc", "log_loss", "brier", "brier_skill"]  # the metrics.json shape
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 0, 1)
    assert (m["rows"], m["surges"], m["surge_rate"], m["threshold"]) == (4, 2, 0.5, 0.7)
    assert m["f1"] == pytest.approx(0.8) and m["precision"] == pytest.approx(2 / 3) and m["recall"] == 1.0
    assert m["auc"] == 0.75
    assert m["pr_auc"] == pytest.approx(1 / 2 * 1 + 1 / 2 * 2 / 3)  # each surge's recall step times precision there
    assert m["brier"] == pytest.approx((0.1 ** 2 + 0.8 ** 2 + 0.3 ** 2 + 0.2 ** 2) / 4)
    assert m["brier_skill"] == pytest.approx(1 - 0.195 / 0.25)  # against always forecasting the rate 0.5
    assert m["log_loss"] == pytest.approx(-np.mean(np.log([0.9, 0.2, 0.7, 0.8])))


def test_a_ratio_with_a_zero_denominator_is_none_not_zero():
    nothing_predicted = T.metrics([0.1, 0.2], [1, 0], 0.5)
    assert nothing_predicted["precision"] is None and nothing_predicted["recall"] == 0.0
    no_surge = T.metrics([0.1, 0.2], [0, 0], 0.5)
    assert no_surge["recall"] is None and no_surge["f1"] is None and no_surge["auc"] is None
    assert no_surge["pr_auc"] is None and no_surge["brier_skill"] is None  # undefined, never 0
    assert no_surge["log_loss"] == pytest.approx(-np.mean(np.log([0.9, 0.8]))) and no_surge["surge_rate"] == 0.0


def test_pr_auc_takes_tied_probabilities_as_one_threshold_and_needs_a_surge():
    assert T.pr_auc([0.5, 0.5, 0.1], [1, 0, 0]) == 0.5  # the tie is one step: precision 1/2 at recall 1
    assert T.pr_auc([0.9, 0.1], [1, 0]) == 1.0 and T.pr_auc([0.3, 0.6], [1, 1]) == 1.0
    assert T.pr_auc([0.3, 0.6], [0, 0]) is None


@pytest.mark.parametrize("probability, skill", [
    ([1.0, 0.0], 1.0),     # perfect
    ([0.5, 0.5], 0.0),     # always the surge rate: no skill
    ([0.2, 0.8], -1.56),   # worse than the rate: Brier 0.64 against 0.25
])
def test_brier_skill_compares_with_always_forecasting_the_surge_rate(probability, skill):
    assert T.brier_skill(probability, [1, 0]) == pytest.approx(skill)


def test_brier_skill_is_none_with_one_class_and_the_new_metrics_check_their_input():
    assert T.brier_skill([0.2, 0.4], [1, 1]) is None and T.brier([0.2, 0.4], [1, 1]) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="probabilities must be from 0 to 1"):
        T.brier([1.2], [1])
    with pytest.raises(ValueError, match="labels must be 0 or 1"):
        T.pr_auc([0.5], [2])


def test_counts_match_the_prediction_rule():
    c = T.counts([0.5, 0.49, 0.5], [1, 1, 0], 0.5)
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 0)
