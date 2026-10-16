from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from map_311 import (
    UNMAPPED,
    change_points,
    check_clock,
    clock_offset_hours,
    combine,
    load_category_map,
)

REPO_MAP = Path(__file__).resolve().parents[2] / "docs" / "data" / "category-map.csv"

MAP = """system,source_category,category,storm_group,match,note
legacy,Tree Emergencies,Fallen Tree or Branches,tree,renamed,
legacy,Parking Enforcement,Parking Enforcement,,legacy_only,
legacy,Test Case A,unmapped,,excluded,test record
new,Fallen Tree or Branches,Fallen Tree or Branches,tree,renamed,
new,Pothole,Pothole,,new_only,
"""


def write_map(tmp_path, body=MAP):
    path = tmp_path / "category-map.csv"
    path.write_text(body)
    return path


def legacy_rows(rows):
    return pd.DataFrame(rows, columns=["case_enquiry_id", "open_dt", "closed_dt", "type", "resubmitted_as"]).assign(
        open_dt=lambda d: pd.to_datetime(d["open_dt"]), closed_dt=lambda d: pd.to_datetime(d["closed_dt"])
    )


def new_rows(rows):
    return pd.DataFrame(rows, columns=["case_id", "open_date", "service_name"]).assign(
        open_date=lambda d: pd.to_datetime(d["open_date"])
    )


def test_repo_map_is_valid():
    cmap = load_category_map(REPO_MAP)
    legacy_groups = cmap[(cmap["system"] == "legacy") & (cmap["storm_group"] != "")]
    assert set(legacy_groups["storm_group"]) == {"snow_ice", "flooding", "tree"}


@pytest.mark.parametrize(
    "body, message",
    [
        ("system,source_category\nlegacy,x\n", "missing columns"),
        (MAP + "legacy,Tree Emergencies,Pruning Request,tree,split,\n", "duplicate"),
        (MAP + "legacy,Snow,Snow,snow_ice,guessed,\n", "match must be"),
        (MAP + "legacy,Fog,Fog,fog,legacy_only,\n", "storm_group must be"),
        (MAP + "legacy,Tree in Park,Fallen Tree or Branches,,split,\n", "more than one storm_group"),
        (MAP + "legacy,,Pothole,,renamed,\n", "blank"),
        (MAP + "old,Pothole,Pothole,,renamed,\n", "system must be"),
    ],
)
def test_bad_maps_are_rejected(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        load_category_map(write_map(tmp_path, body))


def test_every_row_is_mapped_or_unmapped(tmp_path):
    cmap = load_category_map(write_map(tmp_path))
    legacy = legacy_rows([
        ["1", "2025-10-01 09:00", None, "Tree Emergencies", None],
        ["2", "2025-10-01 09:00", None, "Parking Enforcement", None],
        ["3", "2025-10-01 09:00", None, "Brand New Legacy Type", None],
        ["4", "2025-10-01 09:00", None, "Test Case A", None],
    ])
    new = new_rows([
        ["BCS-1", "2025-10-28 09:00", "Fallen Tree or Branches"],
        ["BCS-2", "2025-10-28 09:00", None],
        ["BCS-3", "2025-10-28 09:00", "Something Added Later"],
    ])
    combined, _ = combine(legacy, new, cmap)
    assert len(combined) == 7
    assert combined["category"].notna().all()
    by_id = combined.set_index("case_id")
    assert by_id.loc["1", "category"] == by_id.loc["BCS-1", "category"] == "Fallen Tree or Branches"
    assert by_id.loc["1", "storm_group"] == "tree"
    assert pd.isna(by_id.loc["2", "storm_group"])
    assert set(by_id.index[by_id["match"] == UNMAPPED]) == {"3", "BCS-2", "BCS-3"}
    assert by_id.loc["4", "match"] == "excluded"


def test_resubmitted_legacy_copies_are_dropped(tmp_path):
    cmap = load_category_map(write_map(tmp_path))
    legacy = legacy_rows([
        ["1", "2026-06-20 09:00", "2026-06-26 12:18", "Tree Emergencies", "BCS-9"],
        ["2", "2026-06-20 09:00", "2026-06-26 12:18", "Tree Emergencies", "BCS-404"],
    ])
    new = new_rows([["BCS-9", "2026-06-26 12:17", "Fallen Tree or Branches"]])
    combined, stats = combine(legacy, new, cmap)
    assert stats["resubmitted_dropped"] == 1
    assert sorted(combined["case_id"]) == ["2", "BCS-9"]


def test_empty_inputs(tmp_path):
    cmap = load_category_map(write_map(tmp_path))
    combined, stats = combine(legacy_rows([]), new_rows([]), cmap)
    assert combined.empty and stats["resubmitted_dropped"] == 0
    assert change_points(combined).empty
    assert np.isnan(clock_offset_hours(legacy_rows([]), new_rows([]))[0])


def linked(hours_late, n=25):
    closed = pd.date_range("2026-01-10 09:00", periods=n, freq="7D")
    legacy = legacy_rows([[str(i), c, c, "Tree Emergencies", f"BCS-{i}"] for i, c in enumerate(closed)])
    new = new_rows([[f"BCS-{i}", c + pd.Timedelta(hours=hours_late), "Fallen Tree or Branches"] for i, c in enumerate(closed)])
    return legacy, new


def test_clock_check_passes_for_local_times():
    offset, n = check_clock(*linked(-0.02))
    assert n == 25 and abs(offset) < 0.1


def test_clock_check_fails_when_new_times_are_true_utc():
    with pytest.raises(ValueError, match="clock"):
        check_clock(*linked(5))


def test_clock_check_needs_enough_linked_cases():
    offset, n = check_clock(*linked(5, n=3))
    assert n == 3 and offset == 5


def test_change_points(tmp_path):
    cmap = load_category_map(write_map(tmp_path))
    legacy = legacy_rows([
        ["1", "2025-10-20 09:00", None, "Tree Emergencies", None],
        ["2", "2025-10-30 09:00", None, "Tree Emergencies", None],
        ["3", "2025-10-30 09:00", None, "Parking Enforcement", None],
    ])
    new = new_rows([
        ["BCS-0", "2025-08-01 09:00", "Pothole"],
        ["BCS-1", "2025-10-27 15:00", "Fallen Tree or Branches"],
    ])
    combined, _ = combine(legacy, new, cmap)
    cp = change_points(combined).set_index(["level", "name"])
    row = cp.loc[("category", "Fallen Tree or Branches")]
    assert str(row["switch_date"]) == "2025-10-27"
    assert str(row["legacy_last"]) == "2025-10-30"
    assert row["legacy_after_switch"] == 1
    assert ("storm_group", "tree") in cp.index
    assert ("category", "Parking Enforcement") not in cp.index
