import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import bias_mitigation
from bias_mitigation import comparison_rows, lag_columns, mitigation_table, report
from bias_slicing import unit_attributes
from feature_table import feature_names, relative_names
from test_bias_slicing import ACS, UNITS, static_dir

CONFIG = Path(__file__).resolve().parents[1] / "config"
GROUPS = ["snow_ice", "tree"]


def features_table(per_resident: dict, relative: dict, start="2016-01-01", end="2025-06-30") -> pd.DataFrame:
    """Features like feature_table writes: every raw lag is per_resident[unit] requests per resident times
    the unit's population, and every self-relative lag is relative[unit]."""
    dates = pd.date_range(start, end)
    frames = []
    for unit in UNITS:
        frame = pd.DataFrame({"unit": unit, "date": dates})
        for name in feature_names(GROUPS):
            frame[name] = per_resident[unit] * ACS[unit][0]
        for name in relative_names(GROUPS):
            frame[name] = relative[unit]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def labels_table(features: pd.DataFrame) -> pd.DataFrame:
    day = (features["date"] - features["date"].min()).dt.days
    return features[["unit", "date"]].assign(surge=pd.array(day % 20 == 0, dtype="boolean"))


def test_lag_columns_pair_each_raw_lag_with_its_self_relative_one():
    pairs = lag_columns(GROUPS)
    assert pairs[0] == ("snow_ice", "last", "snow_ice_last", "snow_ice_rel_last")
    assert pairs[-1] == ("tree", "28", "tree_sum28", "tree_rel28")
    assert len(pairs) == len(feature_names(GROUPS)) == len(relative_names(GROUPS))


def test_rows_keep_labeled_days_with_every_version_and_compute_per_capita(tmp_path):
    units = unit_attributes(static_dir(tmp_path))
    features = features_table({u: 0.001 for u in UNITS}, {u: 1.5 for u in UNITS})
    features.loc[0, "tree_rel7"] = np.nan  # no baseline yet: the row is left out of every version
    labels = labels_table(features)
    labels.loc[1, "surge"] = pd.NA  # unlabeled
    rows = comparison_rows(features, labels, units, GROUPS)
    assert len(rows) == len(features) - 2
    assert rows["tree_sum7|per capita"].iloc[0] == pytest.approx(1.0)  # 0.001 per resident, per 1,000 residents
    assert rows["tree_sum7|self-relative"].iloc[0] == 1.5


@pytest.mark.parametrize("drop, message", [("tree_rel7", "features are missing columns"),
                                           ("tree_sum7", "features are missing columns")])
def test_features_without_a_lag_are_rejected(tmp_path, drop, message):
    units = unit_attributes(static_dir(tmp_path))
    features = features_table({u: 0.001 for u in UNITS}, {u: 1.0 for u in UNITS})
    with pytest.raises(ValueError, match=message):
        comparison_rows(features.drop(columns=drop), labels_table(features), units, GROUPS)


def test_empty_or_malformed_inputs_are_rejected(tmp_path):
    units = unit_attributes(static_dir(tmp_path))
    features = features_table({u: 0.001 for u in UNITS}, {u: 1.0 for u in UNITS})
    with pytest.raises(ValueError, match="need rows"):
        comparison_rows(features.iloc[0:0], labels_table(features), units, GROUPS)
    with pytest.raises(ValueError, match="labels need unit, date and surge"):
        comparison_rows(features, labels_table(features).drop(columns="surge"), units, GROUPS)


def test_the_reporting_gap_shows_per_capita_and_goes_away_self_relative(tmp_path):
    # Poor and Crowded (the higher-poverty half) file half as many requests per resident; relative to their own
    # history their storms look the same as everyone's.
    units = unit_attributes(static_dir(tmp_path))
    features = features_table({"Crowded": 0.001, "Poor": 0.001, "Quiet": 0.002, "Rich": 0.002}, {u: 2.0 for u in UNITS})
    rows = comparison_rows(features, labels_table(features), units, GROUPS)
    table = mitigation_table(rows, units, GROUPS, ratio_min=0.8)
    poverty = table[(table["slice"] == "poverty_share") & (table["window"] == "7") & (table["storm_group"] == "tree")
                    & (table["period"] == "train 2015 to 2023") & (table["days"] == "all labeled days")]
    by_version = poverty.set_index("version")
    assert by_version.loc["per capita", "ratio"] == pytest.approx(0.5) and by_version.loc["per capita", "gap"]
    assert by_version.loc["per capita", "lower_group"] == "higher"
    assert by_version.loc["self-relative", "ratio"] == pytest.approx(1.0) and not by_version.loc["self-relative", "gap"]
    assert set(table["period"]) == {"train 2015 to 2023", "validate 2024", "2025 onward"}
    assert set(table["days"]) == {"all labeled days", "surge days"}


def test_the_report_counts_gaps_by_version(tmp_path):
    units = unit_attributes(static_dir(tmp_path))
    features = features_table({"Crowded": 0.001, "Poor": 0.001, "Quiet": 0.002, "Rich": 0.002}, {u: 2.0 for u in UNITS})
    rows = comparison_rows(features, labels_table(features), units, GROUPS)
    text = report(mitigation_table(rows, units, GROUPS, 0.8), rows, 0.8)
    assert "## Gaps by version" in text and "| all labeled days | self-relative | 0 |" in text
    assert "## 7-day sums, surge days" in text
    assert "No group had rows" in report(pd.DataFrame(), rows, 0.8)


def test_main_writes_the_table_and_the_report(tmp_path, monkeypatch, capsys):
    static = static_dir(tmp_path)
    features = features_table({"Crowded": 0.001, "Poor": 0.001, "Quiet": 0.002, "Rich": 0.002}, {u: 2.0 for u in UNITS})
    features.to_parquet(tmp_path / "features.parquet")
    labels_table(features).to_parquet(tmp_path / "labels.parquet")
    out = tmp_path / "reports" / "bias"
    monkeypatch.setattr(sys, "argv", [
        "bias_mitigation.py", "--features", str(tmp_path / "features.parquet"), "--labels", str(tmp_path / "labels.parquet"),
        "--static-dir", str(static), "--config", str(CONFIG / "bias_slicing.csv"),
        "--settings", str(CONFIG / "labels_features.csv"), "--out-dir", str(out)])
    bias_mitigation.main()
    assert {p.name for p in out.iterdir()} == {"mitigation.csv", "mitigation.md"}
    assert "self-relative 0" in capsys.readouterr().out
    assert {"slice", "period", "days", "version", "ratio", "gap"} <= set(pd.read_csv(out / "mitigation.csv").columns)
