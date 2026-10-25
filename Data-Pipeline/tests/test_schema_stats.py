import json

import numpy as np
import pandas as pd
import pytest

from schema_stats import MAX_CATEGORIES, drift, load_thresholds, run, table_stats

THRESHOLDS = {
    "row_count_change_pct": 5.0,
    "missing_change_points": 2.0,
    "mean_shift_std": 0.5,
    "category_share_change_points": 2.0,
}


def located(n=100, tree_share=0.5):
    trees = int(n * tree_share)
    return pd.DataFrame({
        "case_id": [str(i) for i in range(n)],
        "open_dt": pd.to_datetime(["2024-06-01"] * (n // 2) + ["2025-06-01"] * (n - n // 2)),
        "storm_group": ["tree"] * trees + ["snow_ice"] * (n - trees),
        "latitude": np.linspace(42.30, 42.40, n),
        "after_switch": [False] * n,
    })


def test_schema_and_statistics():
    df = located()
    df.loc[:9, "latitude"] = np.nan
    stats = table_stats(df)
    assert stats["rows"] == 100
    assert stats["schema"]["latitude"] == {"dtype": "float64", "missing_pct": 10.0}
    assert stats["categories"]["storm_group"] == {"snow_ice": 50.0, "tree": 50.0}
    assert stats["categories"]["after_switch"] == {"False": 100.0}
    assert "case_id" not in stats["categories"]  # one value per row
    assert set(stats["numeric"]["latitude"]) == {"mean", "std", "min", "p25", "p50", "p75", "max"}
    assert stats["rows_per_year"] == {"2024": 50, "2025": 50}
    assert stats["category_shares_of"] == "rows"


def test_count_tables_weight_shares_by_requests():
    counts = pd.DataFrame({"storm_group": ["tree", "snow_ice", "tree", "snow_ice"], "requests": [3, 1, 0, 0]})
    stats = table_stats(counts)
    assert stats["categories"]["storm_group"] == {"snow_ice": 25.0, "tree": 75.0}
    assert stats["category_shares_of"] == "requests"


def test_columns_with_many_values_get_no_shares():
    df = pd.DataFrame({"code": [f"c{i}" for i in range(MAX_CATEGORIES + 1)]})
    assert table_stats(df)["categories"] == {}


def test_empty_table():
    stats = table_stats(located(0))
    assert stats["rows"] == 0
    assert stats["numeric"]["latitude"]["mean"] is None
    assert stats["categories"].get("storm_group", {}) == {}


def test_no_flags_within_thresholds():
    assert drift(table_stats(located(100)), table_stats(located(103)), THRESHOLDS) == []


def test_each_kind_of_shift_is_flagged():
    before = located(100)
    after = located(110, tree_share=0.8).drop(columns="after_switch").assign(latitude=lambda d: d["latitude"] + 1, depth=1)
    after.loc[:9, "case_id"] = None
    kinds = {f["kind"] for f in drift(table_stats(before), table_stats(after), THRESHOLDS)}
    assert kinds == {"row_count", "column_removed", "column_added", "missing_share", "mean_shift", "category_share"}


def test_type_change_is_flagged():
    before, after = located(), located().assign(latitude=lambda d: d["latitude"].astype(str))
    flags = drift(table_stats(before), table_stats(after), THRESHOLDS)
    assert {"kind": "dtype_changed", "column": "latitude"}.items() <= flags[0].items()


def test_growth_from_an_empty_table_is_flagged():
    flags = drift(table_stats(located(0)), table_stats(located(10)), THRESHOLDS)
    assert flags[0]["kind"] == "row_count"


def write(tmp_path, df, name="located"):
    path = tmp_path / f"{name}.parquet"
    df.to_parquet(path, index=False)
    return path


def test_first_run_then_unchanged_run_writes_nothing(tmp_path):
    table = write(tmp_path, located())
    out = tmp_path / "stats"
    first = run({"located": table}, THRESHOLDS, out, docs=False)
    assert first["changed"] and first["first_run"] == ["located"]
    assert json.loads((out / "drift.json").read_text())["flags"] == []
    stamp = (out / "located.json").stat().st_mtime_ns
    second = run({"located": table}, THRESHOLDS, out, docs=False)
    assert not second["changed"]
    assert (out / "located.json").stat().st_mtime_ns == stamp


def test_changed_data_is_compared_with_the_previous_run(tmp_path):
    out = tmp_path / "stats"
    run({"located": write(tmp_path, located(100))}, THRESHOLDS, out, docs=False)
    result = run({"located": write(tmp_path, located(200, tree_share=0.9))}, THRESHOLDS, out, docs=False)
    report = json.loads((out / "drift.json").read_text())
    assert report["compared"] == ["located"]
    assert {f["kind"] for f in result["flags"]} == {"row_count", "category_share"}
    assert json.loads((out / "located.json").read_text())["rows"] == 200


def test_missing_table_is_skipped(tmp_path):
    out = tmp_path / "stats"
    result = run({"located": write(tmp_path, located()), "daily_counts": tmp_path / "nope.parquet"}, THRESHOLDS, out, docs=False)
    assert result["skipped"] == ["daily_counts"]
    assert not (out / "daily_counts.json").exists()


def test_data_docs_are_built(tmp_path):
    out = tmp_path / "stats"
    result = run({"located": write(tmp_path, located())}, THRESHOLDS, out)
    assert result["data_docs"] == out / "data_docs" / "index.html"
    assert result["data_docs"].exists()


def test_thresholds_config(tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("name,value\n" + "".join(f"{k},{v}\n" for k, v in THRESHOLDS.items()))
    assert load_thresholds(good) == THRESHOLDS
    bad = tmp_path / "bad.csv"
    bad.write_text("name,value\nrow_count_change_pct,lots\n")
    with pytest.raises(ValueError, match="not a number"):
        load_thresholds(bad)
    short = tmp_path / "short.csv"
    short.write_text("name,value\nrow_count_change_pct,5\n")
    with pytest.raises(ValueError, match="missing thresholds"):
        load_thresholds(short)

