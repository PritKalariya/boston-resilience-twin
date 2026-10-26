import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import surge_labels
from daily_counts import STORM_GROUPS
from surge_labels import (
    Rule,
    build_labels,
    check_counts,
    grid_table,
    group_baselines,
    group_flags,
    load_rules,
    load_settings,
    unit_table,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"
SD = Rule("sd_k2_m3", "sd", 2.0, 3)
Q90 = Rule("q90_m1", "q90", None, 1)
LABEL_COLUMNS = ["unit", "date", "surge", "surge_groups", "rule_id", "history_days"] + [
    f"{name}_{g}" for g in STORM_GROUPS for name in ("requests", "mean", "sd", "q90", "surge")
]


def counts_table(units=("East", "West"), days=800, value=1, start="2015-01-01"):
    """A zero-filled daily counts table like daily_counts.py writes, with the same count every day."""
    dates = pd.date_range(start, periods=days, freq="D")
    index = pd.MultiIndex.from_product([list(units), dates, STORM_GROUPS], names=["unit", "date", "storm_group"])
    return index.to_frame(index=False).assign(requests=value, baseline_requests=value)


def set_count(df, unit, date, group, requests, baseline=None):
    hit = (df["unit"] == unit) & (df["date"] == pd.Timestamp(date)) & (df["storm_group"] == group)
    assert hit.sum() == 1
    df.loc[hit, "requests"] = requests
    df.loc[hit, "baseline_requests"] = requests if baseline is None else baseline
    return df


def labels_for(df, rule=SD, groups=STORM_GROUPS, **kwargs):
    return build_labels(group_baselines(check_counts(df)), rule, list(groups), **kwargs)


def cell(labels, unit, date):
    hit = labels[(labels["unit"] == unit) & (labels["date"] == pd.Timestamp(date))]
    assert len(hit) == 1
    return hit.iloc[0]


def test_rule_math_per_group():
    cells = pd.DataFrame({
        "requests": [5, 4, 3, 2, 7],
        "mean": [2.0, 2.0, 0.3, 0.3, np.nan],
        "sd": [1.0, 1.0, 0.5, 0.5, np.nan],
        "q90": [4.0, 4.0, 0.0, 0.0, np.nan],
    })
    # sd, k 2, m 3: line at mean + 2 sd, strictly above it, and at least 3 requests.
    expected_sd = pd.Series([True, False, True, False, pd.NA], dtype="boolean")
    pd.testing.assert_series_equal(group_flags(cells, SD), expected_sd, check_names=False)
    # q90, m 1: strictly above the 90th percentile of the history.
    expected_q90 = pd.Series([True, False, True, True, pd.NA], dtype="boolean")
    pd.testing.assert_series_equal(group_flags(cells, Q90), expected_q90, check_names=False)


def test_labels_start_once_there_are_20_history_days():
    labels = labels_for(counts_table())
    assert labels.loc[labels["date"].dt.year == 2015, "surge"].isna().all()
    assert labels.loc[labels["surge"].notna(), "date"].min() == pd.Timestamp("2016-01-06")
    assert cell(labels, "East", "2016-01-05")["history_days"] == 19
    assert cell(labels, "East", "2016-01-06")["history_days"] == 20


def test_no_baseline_means_no_label_not_false():
    first = cell(labels_for(counts_table()), "East", "2015-06-01")
    assert pd.isna(first["surge"]) and pd.isna(first["surge_groups"])
    assert all(pd.isna(first[f"surge_{g}"]) for g in STORM_GROUPS)


def test_a_spike_is_a_surge_and_names_its_group():
    df = set_count(counts_table(), "East", "2016-03-01", "tree", 10)
    labels = labels_for(df)
    east = cell(labels, "East", "2016-03-01")
    assert bool(east["surge"])
    assert east["surge_groups"] == "tree"
    assert bool(east["surge_tree"]) and not bool(east["surge_snow_ice"])
    west = cell(labels, "West", "2016-03-01")
    assert not bool(west["surge"]) and west["surge_groups"] == ""


def test_m_is_a_floor():
    df = set_count(counts_table(), "East", "2016-03-01", "tree", 2)
    assert not bool(cell(labels_for(df), "East", "2016-03-01")["surge"])
    assert bool(cell(labels_for(df, rule=Rule("sd_k2_m1", "sd", 2.0, 1)), "East", "2016-03-01")["surge"])


def test_any_group_makes_a_surge_and_label_groups_decide_which_count():
    df = counts_table()
    set_count(df, "East", "2016-03-01", "flooding", 10)
    set_count(df, "West", "2016-03-02", "snow_ice", 10)
    set_count(df, "West", "2016-03-02", "tree", 10)
    labels = labels_for(df)
    assert cell(labels, "East", "2016-03-01")["surge_groups"] == "flooding"
    assert cell(labels, "West", "2016-03-02")["surge_groups"] == "snow_ice;tree"
    without_flooding = labels_for(df, groups=["snow_ice", "tree"])
    east = cell(without_flooding, "East", "2016-03-01")
    assert not bool(east["surge"]) and east["surge_groups"] == ""
    assert bool(east["surge_flooding"])  # still reported next to the label


def test_baseline_comes_from_baseline_requests():
    df = counts_table(value=2).assign(baseline_requests=1)
    labels = labels_for(df, rule=Rule("sd_k2_m1", "sd", 2.0, 1))
    labeled = labels["surge"].dropna()
    assert len(labeled) > 0 and labeled.astype(bool).all()


def test_q90_rule_end_to_end():
    df = set_count(counts_table(), "East", "2016-03-01", "tree", 2)
    assert bool(cell(labels_for(df, rule=Q90), "East", "2016-03-01")["surge"])
    assert not bool(cell(labels_for(df, rule=SD), "East", "2016-03-01")["surge"])


def test_a_date_range_keeps_the_earlier_history():
    df = set_count(counts_table(), "East", "2016-06-10", "tree", 10)
    full = labels_for(df)
    part = labels_for(df, start="2016-06-01", end="2016-06-30")
    assert len(part) == 2 * 30
    expected = full[(full["date"] >= "2016-06-01") & (full["date"] <= "2016-06-30")].reset_index(drop=True)
    pd.testing.assert_frame_equal(part, expected)
    assert bool(cell(part, "East", "2016-06-10")["surge"])


def test_same_counts_give_the_same_labels_in_any_row_order():
    df = set_count(counts_table(), "East", "2016-03-01", "tree", 10)
    first = labels_for(df)
    pd.testing.assert_frame_equal(first, labels_for(df.sample(frac=1, random_state=7)))
    pd.testing.assert_frame_equal(first, labels_for(df))


def test_label_columns_and_one_row_per_unit_and_day():
    labels = labels_for(counts_table(days=400))
    assert list(labels.columns) == LABEL_COLUMNS
    assert len(labels) == 2 * 400 and not labels.duplicated(["unit", "date"]).any()
    assert set(labels["rule_id"]) == {"sd_k2_m3"}


def test_empty_counts_give_an_empty_table_and_grid():
    empty = counts_table().iloc[0:0]
    labels = labels_for(empty)
    assert labels.empty and list(labels.columns) == LABEL_COLUMNS
    rules, _ = load_rules(CONFIG / "surge_rules.csv")
    grid = grid_table(group_baselines(check_counts(empty)), rules, list(STORM_GROUPS))
    assert len(grid) == len(rules) and (grid["labeled cells"] == 0).all()


def test_missing_columns_are_named():
    with pytest.raises(ValueError, match="missing columns.*baseline_requests"):
        check_counts(counts_table().drop(columns="baseline_requests"))


def _negative(df):
    df.loc[0, "requests"] = -1
    return df


def _fraction(df):
    df["requests"] = df["requests"].astype(float)
    df.loc[0, "requests"] = 1.5
    return df


def _unknown_group(df):
    df.loc[0, "storm_group"] = "hail"
    return df


def _repeated_row(df):
    return pd.concat([df, df.iloc[[0]]], ignore_index=True)


def _missing_day(df):
    return df.drop(index=5).reset_index(drop=True)


def _baseline_above_requests(df):
    df.loc[0, "baseline_requests"] = 5
    return df


def _no_unit(df):
    df["unit"] = df["unit"].astype(object)
    df.loc[0, "unit"] = None
    return df


def _bad_date(df):
    df["date"] = df["date"].astype(object)
    df.loc[0, "date"] = "not a date"
    return df


def _time_of_day(df):
    df.loc[0, "date"] = df.loc[0, "date"] + pd.Timedelta(hours=12)
    return df


@pytest.mark.parametrize(
    "break_it, message",
    [
        (_negative, "whole numbers"),
        (_fraction, "whole numbers"),
        (_unknown_group, "unknown storm groups"),
        (_repeated_row, "repeat"),
        (_missing_day, "every unit needs every storm group"),
        (_baseline_above_requests, "baseline_requests is above requests"),
        (_no_unit, "no unit, date or storm group"),
        (_bad_date, "not a date"),
        (_time_of_day, "time of day"),
    ],
)
def test_malformed_counts_are_rejected(break_it, message):
    with pytest.raises(ValueError, match=message):
        check_counts(break_it(counts_table(days=60)))


def test_repo_rules_parse_with_one_chosen():
    rules, chosen = load_rules(CONFIG / "surge_rules.csv")
    assert chosen.rule_id == "sd_k2_m5"
    assert {r.kind for r in rules} == {"sd", "q90"}
    assert all(1.5 <= r.k <= 2 for r in rules if r.kind == "sd")
    assert all(1 <= r.m <= 5 for r in rules)


HEADER = "rule_id,rule,k,m,chosen,note\n"


@pytest.mark.parametrize(
    "body, message",
    [
        ("a,sd,2,3,no,\n", "exactly one"),
        ("a,sd,2,3,yes,\nb,sd,2,4,yes,\n", "exactly one"),
        ("a,zscore,2,3,yes,\n", "unknown rule"),
        ("a,sd,,3,yes,\n", "needs k above 0"),
        ("a,sd,0,3,yes,\n", "needs k above 0"),
        ("a,q90,2,3,yes,\n", "takes no k"),
        ("a,sd,2,0,yes,\n", "needs m"),
        ("a,sd,2,x,yes,\n", "needs m"),
        ("a,sd,2,3,yes,\na,sd,2,4,no,\n", "repeats rule_id"),
        ("a,sd,2,3,maybe,\n", "use yes or no"),
        ("", "lists no rules"),
    ],
)
def test_bad_rules_are_rejected(tmp_path, body, message):
    path = tmp_path / "rules.csv"
    path.write_text(HEADER + body)
    with pytest.raises(ValueError, match=message):
        load_rules(path)


def test_rules_file_without_a_column_is_rejected(tmp_path):
    path = tmp_path / "rules.csv"
    path.write_text("rule_id,rule,k,m\na,sd,2,3\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_rules(path)


def test_repo_settings():
    settings = load_settings(CONFIG / "labels_features.csv")
    assert settings == {
        "label_groups": ["snow_ice", "tree", "flooding"],
        "feature_groups": ["snow_ice", "tree"],
        "feature_min_lag_days": 2,
        "forecast_cutoff_local": "18:00",
        "forecast_issue_margin_hours": 6,
        "observed_weather_lag_days": 2,
    }


SETTINGS_OK = {"label_groups": "snow_ice;tree;flooding", "feature_groups": "snow_ice;tree", "feature_min_lag_days": "2",
               "forecast_cutoff_local": "18:00", "forecast_issue_margin_hours": "6", "observed_weather_lag_days": "2"}


@pytest.mark.parametrize(
    "change, message",
    [
        ({"label_groups": "snow_ice;hail"}, "unknown storm groups"),
        ({"label_groups": ""}, "names no storm group"),
        ({"feature_groups": "tree;tree"}, "repeats a storm group"),
        ({"feature_min_lag_days": "0"}, "1 or more"),
        ({"feature_min_lag_days": "two"}, "1 or more"),
        ({"feature_groups": None}, "missing settings"),
        ({"forecast_cutoff_local": "6pm"}, "HH:MM"),
        ({"forecast_cutoff_local": "24:00"}, "HH:MM"),
        ({"forecast_cutoff_local": None}, "missing settings"),
        ({"forecast_issue_margin_hours": "25"}, "0 to 24"),
        ({"forecast_issue_margin_hours": "six"}, "0 to 24"),
        ({"forecast_issue_margin_hours": None}, "missing settings"),
        ({"observed_weather_lag_days": "1"}, "2 or more"),
        ({"observed_weather_lag_days": None}, "missing settings"),
    ],
)
def test_bad_settings_are_rejected(tmp_path, change, message):
    values = {**SETTINGS_OK, **change}
    lines = ["setting,value"] + [f"{k},{v}" for k, v in values.items() if v is not None]
    path = tmp_path / "settings.csv"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match=message):
        load_settings(path)


def test_repeated_setting_is_rejected(tmp_path):
    path = tmp_path / "settings.csv"
    path.write_text("setting,value\nlabel_groups,tree\nlabel_groups,snow_ice\nfeature_groups,tree\nfeature_min_lag_days,2\n")
    with pytest.raises(ValueError, match="repeats settings"):
        load_settings(path)


def test_grid_has_one_row_per_rule_from_the_same_cells():
    df = counts_table()
    set_count(df, "East", "2016-03-01", "tree", 10)
    set_count(df, "West", "2016-04-01", "flooding", 10)
    set_count(df, "West", "2016-05-01", "tree", 2)  # a surge for q90_m1 only
    rules, _ = load_rules(CONFIG / "surge_rules.csv")
    grid = grid_table(group_baselines(check_counts(df)), rules, list(STORM_GROUPS)).set_index("rule")
    assert list(grid.index) == [r.rule_id for r in rules]
    assert grid["labeled cells"].nunique() == 1
    assert grid.loc["sd_k2_m3", "surges"] == 2
    assert grid.loc["sd_k2_m3", "flooding only"] == 1
    assert grid.loc["q90_m1", "surges"] == 3
    assert grid.loc["sd_k2_m3", "units with no train surge"] == 0
    assert {"train 2015 to 2023", "validate 2024", "2025 onward", "train in spec range"} <= set(grid.columns)


def test_unit_table_reports_coverage_and_rate():
    df = set_count(counts_table(), "East", "2016-03-01", "tree", 10)
    table = unit_table(labels_for(df)).set_index("unit")
    assert list(table.index) == ["East", "West"]
    assert table.loc["East", "label coverage"] == table.loc["West", "label coverage"]
    assert table.loc["West", "train 2015 to 2023"] == "0.00%"


def test_main_writes_labels_and_prints_the_grid(tmp_path, monkeypatch, capsys):
    counts = set_count(counts_table(), "East", "2016-03-01", "tree", 10)
    counts.to_parquet(tmp_path / "counts.parquet", index=False)
    argv = ["surge_labels.py", "--counts", str(tmp_path / "counts.parquet"),
            "--rules", str(CONFIG / "surge_rules.csv"), "--settings", str(CONFIG / "labels_features.csv"),
            "--out", str(tmp_path / "labels.parquet"), "--grid"]
    monkeypatch.setattr(sys, "argv", argv)
    surge_labels.main()
    written = pd.read_parquet(tmp_path / "labels.parquet")
    assert len(written) == 2 * 800
    assert bool(cell(written, "East", "2016-03-01")["surge"])
    out = capsys.readouterr().out
    assert "## Surge rate per rule" in out and "q90_m3" in out and "## Per neighborhood" in out


def test_main_names_a_missing_column(tmp_path, monkeypatch):
    counts_table().drop(columns="requests").to_parquet(tmp_path / "counts.parquet", index=False)
    argv = ["surge_labels.py", "--counts", str(tmp_path / "counts.parquet"),
            "--rules", str(CONFIG / "surge_rules.csv"), "--settings", str(CONFIG / "labels_features.csv"),
            "--out", str(tmp_path / "labels.parquet")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="missing columns"):
        surge_labels.main()
