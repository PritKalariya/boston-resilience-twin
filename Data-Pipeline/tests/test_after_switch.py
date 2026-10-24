from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from after_switch import (
    OVERLAP_COLUMNS,
    POLICIES,
    RATE,
    apply_policy,
    by_unit,
    load_policy,
    overlap_table,
    policy_panels,
    surge_cells,
    window_summary,
)

SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config" / "processing.csv"
STORM_GROUPS = ["snow_ice", "tree", "flooding"]


def config(tmp_path, text):
    path = tmp_path / "processing.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_config_defaults_to_keep():
    assert load_policy(SHIPPED_CONFIG) == "keep"


@pytest.mark.parametrize("policy", POLICIES)
def test_load_policy_reads_every_option(tmp_path, policy):
    path = config(tmp_path, f"setting,value,options,note\nother_setting,x,,\nafter_switch_policy, {policy} ,,\n")
    assert load_policy(path) == policy


def test_load_policy_accepts_a_byte_order_mark(tmp_path):
    path = tmp_path / "processing.csv"
    path.write_text("setting,value\nafter_switch_policy,drop\n", encoding="utf-8-sig")
    assert load_policy(path) == "drop"


def test_load_policy_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "processing.csv")


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "is empty"),
        ("\n\n", "is empty"),
        ("setting,value\n", "no after_switch_policy row"),
        ("setting,value\nother_setting,keep\n", "no after_switch_policy row"),
        ("name,value\nafter_switch_policy,keep\n", r"missing columns: \['setting'\]"),
        ("setting,value\nafter_switch_policy,delete\n", "unknown after_switch_policy 'delete'"),
        ("setting,value\nafter_switch_policy,Keep\n", "unknown after_switch_policy 'Keep'"),
        ("setting,value\nafter_switch_policy,\n", "unknown after_switch_policy ''"),
        ("setting,value\nafter_switch_policy,keep\nafter_switch_policy,drop\n", "2 times"),
        ("setting,value\nafter_switch_policy,keep,x\n", "more fields than the header"),
        ("setting,value\nafter_switch_policy,keep,drop,x\n", "more fields than the header"),
        ("setting,value\nother_setting,1\nafter_switch_policy,keep,x\n", "could not be read"),
        ("setting,value,note\nafter_switch_policy\n", "unknown after_switch_policy ''"),
    ],
)
def test_load_policy_rejects_bad_config(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_policy(config(tmp_path, text))


def test_load_policy_rejects_a_binary_file(tmp_path):
    path = tmp_path / "processing.csv"
    path.write_bytes(b"\xff\xfe\x00s\x00e\x00t")
    with pytest.raises(ValueError, match="could not be read"):
        load_policy(path)


def requests(flags):
    return pd.DataFrame({"case_id": range(len(flags)), "after_switch": pd.Series(flags, dtype=bool)})


def test_keep_counts_every_row_on_its_day_and_in_baselines():
    out = apply_policy(requests([False, True, False]), "keep")
    assert out["case_id"].tolist() == [0, 1, 2]
    assert out["in_baseline"].tolist() == [True, True, True]


def test_drop_removes_after_switch_rows():
    out = apply_policy(requests([False, True, False]), "drop")
    assert out["case_id"].tolist() == [0, 2]
    assert out["in_baseline"].tolist() == [True, True]


def test_exclude_from_baselines_keeps_the_rows_out_of_baselines_only():
    out = apply_policy(requests([False, True, False]), "exclude_from_baselines")
    assert out["case_id"].tolist() == [0, 1, 2]
    assert out["in_baseline"].tolist() == [True, False, True]


def test_apply_policy_leaves_its_input_alone():
    df = requests([False, True])
    for policy in POLICIES:
        apply_policy(df, policy)
    assert list(df.columns) == ["case_id", "after_switch"] and len(df) == 2


@pytest.mark.parametrize("policy", POLICIES)
def test_apply_policy_empty_input(policy):
    out = apply_policy(requests([]), policy)
    assert out.empty
    assert "in_baseline" in out.columns


def test_apply_policy_unknown_policy():
    with pytest.raises(ValueError, match="unknown after_switch_policy 'remove'"):
        apply_policy(requests([False]), "remove")


def test_apply_policy_without_the_flag_column():
    with pytest.raises(ValueError, match="no after_switch column"):
        apply_policy(pd.DataFrame({"case_id": [1]}), "keep")


def test_apply_policy_with_missing_flags():
    df = pd.DataFrame({"after_switch": pd.Series([True, None, None], dtype="boolean")})
    with pytest.raises(ValueError, match="missing on 2 rows"):
        apply_policy(df, "keep")


def test_apply_policy_with_flags_that_are_not_true_or_false():
    with pytest.raises(ValueError, match="true or false"):
        apply_policy(pd.DataFrame({"after_switch": ["no", "yes"]}), "drop")


def combined(rows):
    columns = ["open_dt", "source_system", "category", "storm_group", "after_switch"]
    return pd.DataFrame(rows, columns=columns).astype({"open_dt": "datetime64[ns]", "after_switch": bool})


def test_overlap_window_counts_both_systems():
    df = combined([
        ("2025-01-05 09:00", "legacy", "Pruning Request", "tree", False),
        ("2025-01-10 08:00", "new", "Pruning Request", "tree", False),  # switch day
        ("2025-01-10 10:00", "legacy", "Pruning Request", "tree", True),
        ("2025-01-12 10:00", "new", "Pruning Request", "tree", False),
        ("2025-01-19 23:00", "legacy", "Pruning Request", "tree", True),  # last legacy row, day 10
        ("2025-01-25 10:00", "new", "Pruning Request", "tree", False),  # after the window
        ("2025-01-02 10:00", "legacy", "Pothole", "", False),
        ("2025-01-03 10:00", "new", "Pothole", "", False),  # switched, no legacy row after it
    ])
    assert overlap_table(df).to_dict("records") == [{
        "category": "Pruning Request",
        "storm_group": "tree",
        "switch_date": date(2025, 1, 10),
        "last_legacy": date(2025, 1, 19),
        "days": 10,
        "legacy_after_switch": 2,
        "new_in_window": 2,
        "legacy_share_pct": 50.0,
    }]


def test_overlap_sorted_by_after_switch_rows():
    df = combined([
        ("2025-01-01 08:00", "new", "A", "", False),
        ("2025-01-02 08:00", "legacy", "A", "", True),
        ("2025-01-01 08:00", "new", "B", None, False),
        ("2025-01-02 08:00", "legacy", "B", None, True),
        ("2025-01-03 08:00", "legacy", "B", None, True),
    ])
    table = overlap_table(df)
    assert table["category"].tolist() == ["B", "A"]
    assert table["storm_group"].tolist() == ["", ""]


def test_overlap_empty_input():
    table = overlap_table(combined([]))
    assert table.empty and list(table.columns) == OVERLAP_COLUMNS


def test_overlap_flag_without_new_rows_is_an_error():
    df = combined([("2025-01-10 10:00", "legacy", "Pruning Request", "tree", True)])
    with pytest.raises(ValueError, match="no new-system rows"):
        overlap_table(df)


def located(rows):
    columns = ["open_dt", "neighborhood", "storm_group", "after_switch"]
    return pd.DataFrame(rows, columns=columns).astype({"open_dt": "datetime64[ns]", "after_switch": bool})


def test_policy_panels_count_rows_and_baseline_rows():
    df = located([
        ("2024-01-02 08:00", "Roxbury", "tree", False),
        ("2024-01-02 09:00", "Roxbury", "tree", True),
        ("2024-01-02 10:00", "Roxbury", "tree", True),
        ("2024-01-03 10:00", "Fenway", "tree", True),
    ])
    days = pd.date_range("2024-01-01", "2024-01-03")
    cells = [(pd.Timestamp("2024-01-02"), "Roxbury"), (pd.Timestamp("2024-01-03"), "Fenway")]
    # policy: (counted on the day, counted in baselines) for the two cells
    expected = {"keep": ([3, 1], [3, 1]), "drop": ([1, 0], [1, 0]), "exclude_from_baselines": ([3, 1], [1, 0])}
    for policy, (counted, in_baseline) in expected.items():
        counts, base_counts = policy_panels(df, policy, "neighborhood", False, ["Fenway", "Roxbury"], days)
        assert [counts.loc[cell] for cell in cells] == counted
        assert [base_counts.loc[cell] for cell in cells] == in_baseline


def test_exclude_from_baselines_only_matters_a_year_later():
    # Five after_switch rows a day for 29 days in 2022 and nothing else, then three
    # ordinary rows on 2023-07-01, whose baseline is exactly those 29 days a year back.
    window = pd.date_range("2022-06-17", "2022-07-15")
    rows = [(f"{day.date()} 10:00", "Roxbury", "tree", True) for day in window for _ in range(5)]
    rows += [("2023-07-01 10:00", "Roxbury", "tree", False)] * 3
    days = pd.date_range("2019-01-01", "2023-12-31")
    flags = {}
    for policy in POLICIES:
        counts, base_counts = policy_panels(located(rows), policy, "neighborhood", False, ["Roxbury"], days)
        flags[policy] = surge_cells(counts, base_counts)[0]["Roxbury"]
    # Inside the window a baseline only looks a year or more back, so exclude_from_baselines acts like keep.
    assert flags["keep"][window].all() and flags["exclude_from_baselines"][window].all()
    assert not flags["drop"][window].any()
    # A year later the window is baseline history: keep's raised baseline hides the
    # three requests, while drop and exclude_from_baselines flag them.
    day = pd.Timestamp("2023-07-01")
    assert not flags["keep"][day]
    assert flags["drop"][day] and flags["exclude_from_baselines"][day]


def test_surge_cells_keep_the_panel_columns():
    days = pd.date_range("2020-01-01", periods=800)
    columns = pd.MultiIndex.from_product([["Roxbury"], STORM_GROUPS], names=["neighborhood", "storm_group"])
    counts = pd.DataFrame(0, index=days, columns=columns)
    flags, labeled, expected = surge_cells(counts, counts)
    for frame in (flags, labeled, expected):
        assert frame.columns.equals(columns) and frame.index.equals(days)
    assert not labeled.iloc[0].any() and labeled.iloc[-1].all()


def test_by_unit_adds_counts_and_flags_a_unit_when_any_group_surges():
    days = pd.date_range("2024-01-01", periods=2)
    columns = pd.MultiIndex.from_product([["Fenway", "Roxbury"], STORM_GROUPS], names=["neighborhood", "storm_group"])
    counts = pd.DataFrame(1, index=days, columns=columns)
    flags = pd.DataFrame(False, index=days, columns=columns)
    flags.loc[days[1], ("Roxbury", "tree")] = True
    labeled = pd.DataFrame(True, index=days, columns=columns)
    expected = pd.DataFrame(0.5, index=days, columns=columns)
    expected[("Fenway", "flooding")] = np.nan
    total, any_flag, any_labeled, base = by_unit(counts, flags, labeled, expected)
    assert total.loc[days[0]].tolist() == [3, 3]
    assert any_flag["Roxbury"].tolist() == [False, True] and not any_flag["Fenway"].any()
    assert any_labeled.all().all()
    assert base.loc[days[0]].tolist() == [1.0, 1.5]


def test_window_summary_levels_and_rate():
    days = pd.date_range("2024-01-01", periods=4)
    counts = pd.DataFrame({"A": [0, 2, 4, 2], "B": [0, 0, 0, 0]}, index=days)
    flags = pd.DataFrame({"A": [False, False, True, False], "B": [False] * 4}, index=days)
    labeled = pd.DataFrame({"A": [True] * 4, "B": [False, True, True, True]}, index=days)
    expected = pd.DataFrame({"A": [1.0] * 4, "B": [np.nan, 1.0, 1.0, 1.0]}, index=days)
    assert window_summary(counts, flags, labeled, expected, days[1], days[3]) == {
        "days": 3,
        "requests": 8,
        "per neighborhood-day": 1.333,
        "same weeks, previous 3 years": 1.0,
        "ratio": 1.33,
        "labeled cells": 6,
        RATE: 16.67,
        "neighborhoods with a surge": 1,
    }


def test_window_summary_outside_the_data():
    days = pd.date_range("2024-01-01", periods=2)
    frame = pd.DataFrame({"A": [0, 0]}, index=days)
    out = window_summary(frame, frame.astype(bool), frame.astype(bool), frame.astype(float), "2025-01-01", "2025-01-31")
    assert out["days"] == 0 and out["requests"] == 0 and out["labeled cells"] == 0
    assert np.isnan(out["per neighborhood-day"]) and np.isnan(out["ratio"])
