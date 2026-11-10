"""Tests for test results by kind of day (#107): the slice rules, undefined values and the day bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
import slices as L  # noqa: E402
import training_data as D  # noqa: E402

UNITS = ("Alpha", "Beta")


def test_the_committed_rules_load():
    rules = L.load_rules(D.load_settings().slices)
    assert list(rules) == ["storm", "freeze", "heat"]
    assert [rule.input for rule in rules["storm"]] == ["alert_snow_ice_count", "alert_tree_count", "alert_flooding_count",
                                                      "wx_snowfall_mm", "wx_precip_mm", "wx_gust_max_ms"]
    assert (rules["freeze"][0].operator, rules["freeze"][0].value) == ("<=", 0.0)
    assert (rules["heat"][0].input, rules["heat"][0].operator, rules["heat"][0].value) == ("wx_tmax_c", ">=", 32.2)


@pytest.mark.parametrize("text, message", [
    ("", "needs the header"),
    ("slice,input,operator,value\nheat,wx_tmax_c,>=,32\n", "needs the header"),
    ("slice,input,operator,value,note\n", "defines no slice"),
    ("slice,input,operator,value,note\nheat,wx_tmax_c,=>,32,x\n", "operator '=>' is not one of"),
    ("slice,input,operator,value,note\nheat,wx_tmax_c,>=,hot,x\n", "value 'hot' is not a number"),
    ("slice,input,operator,value,note\nheat,wx_tmax_c,>=,nan,x\n", "value 'nan' is not a number"),
    ("slice,input,operator,value,note\nall,wx_tmax_c,>=,32,x\n", "'all' is every test row"),
    ("slice,input,operator,value,note\nheat,,>=,32,x\n", "the slice heat needs an input"),
])
def test_a_bad_rules_file_is_refused_naming_the_row(tmp_path, text, message):
    path = tmp_path / "slices.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        L.load_rules(path)


def test_a_slice_holds_the_rows_where_any_rule_holds_and_a_missing_value_holds_none():
    rows = pd.DataFrame({"snow": [0.0, 30.0, np.nan, 0.0], "gust": [25.0, 0.0, 0.0, np.nan]})
    rules = {"storm": [L.Rule("storm", "snow", ">=", 25.4), L.Rule("storm", "gust", ">=", 20.6)],
             "calm": [L.Rule("calm", "gust", "<", 1.0)]}
    found = L.masks(rows, rules)
    assert found["all"].tolist() == [True] * 4
    assert found["storm"].tolist() == [True, True, False, False]
    assert found["calm"].tolist() == [False, True, True, False]  # gust NaN holds no rule, not even "<"
    with pytest.raises(ValueError, match="the input wind is not in the features"):
        L.masks(rows, {"storm": [L.Rule("storm", "wind", ">=", 1.0)]})


def _year(model_alarm, baseline_alarm, seed=0):
    """A year (2025) of test rows for two units, every season, with heat on 5 July days that have no
    surge and a breeze on 5 August days with no surge but where model_alarm() raises alarms."""
    days = pd.date_range("2025-01-01", "2025-12-31")
    rng = np.random.default_rng(seed)
    frames = []
    for unit in UNITS:
        label = (rng.random(len(days)) < np.where(days.month.isin([1, 2, 12]), 0.3, 0.05)).astype(int)
        heat, breeze = days.isin(pd.date_range("2025-07-10", periods=5)), days.isin(pd.date_range("2025-08-10", periods=5))
        label[heat | breeze] = 0
        frames.append(pd.DataFrame({"unit": unit, "date": days, "label": label, "heat": heat, "breeze": breeze}))
    rows = pd.concat(frames, ignore_index=True)
    model = rows.assign(prediction=model_alarm(rows))
    baseline = rows.assign(prediction=baseline_alarm(rows))
    features = rows[["unit", "date"]].assign(wx_tmax_c=np.where(rows["heat"], 35.0, 20.0),
                                             wind=np.where(rows["breeze"], 15.0, 2.0))
    rules = {"heat": [L.Rule("heat", "wx_tmax_c", ">=", 32.2)], "breeze": [L.Rule("breeze", "wind", ">=", 10.0)]}
    return model, baseline, features, rules


def test_a_slice_with_no_surge_reports_undefined_values_never_zero():
    def model_alarm(rows):  # catches every surge, and alarms on the breeze days too
        return ((rows["label"] == 1) | rows["breeze"]).astype(int)

    def baseline_alarm(rows):  # alarms on nothing
        return pd.Series(0, index=rows.index)
    records = {r["slice"]: r for r in L.slice_table(*_year(model_alarm, baseline_alarm), resamples=200, seed=0)}
    heat = records["heat"]  # no surge and no alarm: no F1, precision or recall
    assert (heat["days"], heat["rows"], heat["surges"]) == (5, 10, 0)
    assert {k: heat["model"][k] for k in ("tp", "fp", "fn", "f1", "precision", "recall")} == {
        "tp": 0, "fp": 0, "fn": 0, "f1": None, "precision": None, "recall": None}
    assert heat["f1_gain"] is None and heat["f1_gain_ci95"] is None and heat["bootstrap_draws_usable"] == 0.0
    breeze = records["breeze"]  # no surge but alarms: F1 and precision are a real 0, recall stays undefined
    assert (breeze["surges"], breeze["model"]["fp"]) == (0, 10)
    assert breeze["model"]["f1"] == 0.0 and breeze["model"]["precision"] == 0.0 and breeze["model"]["recall"] is None
    everything = records["all"]  # the model catches every surge
    assert list(records) == ["all", "heat", "breeze"] and everything["model"]["recall"] == 1.0
    assert everything["baseline"]["f1"] == 0.0 and everything["f1_gain"] == everything["model"]["f1"]


def test_the_bootstrap_draws_whole_days_and_repeats_with_its_seed():
    def model_alarm(rows):
        return rows["label"]

    def baseline_alarm(rows):
        return ((rows["label"] == 1) & (rows["unit"] == "Alpha")).astype(int)  # misses every Beta surge
    model, baseline, features, rules = _year(model_alarm, baseline_alarm)
    first = L.slice_table(model, baseline, features, rules, resamples=300, seed=7)[0]
    assert first["f1_gain"] > 0 and 0 < first["f1_gain_ci95"][0] <= first["f1_gain"] <= first["f1_gain_ci95"][1]
    assert L.slice_table(model, baseline, features, rules, resamples=300, seed=7)[0] == first
    # a copy of every unit gives every day twice the rows but the same day totals' ratios, so the same
    # interval: the draws are of days, not of rows
    twice = [frame.assign(unit=frame["unit"] + "2") for frame in (model, baseline, features)]
    doubled = [pd.concat([a, b], ignore_index=True) for a, b in zip((model, baseline, features), twice)]
    assert L.slice_table(*doubled, rules, resamples=300, seed=7)[0]["f1_gain_ci95"] == pytest.approx(first["f1_gain_ci95"])


def test_the_same_model_twice_gains_nothing():
    def alarm(rows):
        return rows["label"]
    first = L.slice_table(*_year(alarm, alarm), resamples=200, seed=0)[0]
    assert first["f1_gain"] == 0.0 and first["f1_gain_ci95"] == [0.0, 0.0]


def test_the_two_runs_must_hold_the_same_test_rows():
    def alarm(rows):
        return rows["label"]
    model, baseline, features, rules = _year(alarm, alarm)
    with pytest.raises(ValueError, match="do not hold the same test rows"):
        L.slice_table(model.iloc[1:], baseline, features, rules, resamples=100, seed=0)
    flipped = baseline.assign(label=1 - baseline["label"])
    with pytest.raises(ValueError, match="do not hold the same test rows and labels"):
        L.slice_table(model, flipped, features, rules, resamples=100, seed=0)


def test_flat_rows_put_both_models_side_by_side():
    def alarm(rows):
        return rows["label"]
    rows = L.flat(L.slice_table(*_year(alarm, alarm), resamples=100, seed=0))
    assert [row["slice"] for row in rows] == ["all", "heat", "breeze"]
    assert {"model_f1", "baseline_f1", "f1_gain", "f1_gain_ci95_low", "f1_gain_ci95_high"} <= set(rows[0])
    assert rows[1]["model_f1"] is None and rows[1]["f1_gain_ci95_low"] is None
