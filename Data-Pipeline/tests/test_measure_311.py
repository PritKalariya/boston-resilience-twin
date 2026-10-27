import sys

import numpy as np
import pandas as pd

import measure_311
from measure_311 import (
    daily_cells,
    inferred_format_loss,
    legacy_storm_groups,
    load_weather,
    matched_ratio,
    normalize_neighborhood,
    reconcile,
    sparsity,
    storm_ratios,
)

CATEGORY_MAP = (
    "system,source_category,storm_group\n"
    "legacy,Tree Emergencies,tree\n"
    "legacy,Request for Snow Plowing,snow_ice\n"
    "legacy,Pothole Repair,\n"
    "new,Tree Emergency,tree\n"
)


def test_normalize_neighborhood():
    raw = pd.Series(["South Boston", "Brighton", "Allston", "Mattapan", "Boston", None, "", " Dorchester "])
    assert normalize_neighborhood(raw).tolist() == [
        "South Boston / South Boston Waterfront",
        "Allston / Brighton",
        "Allston / Brighton",
        "Greater Mattapan",
        pd.NA,
        pd.NA,
        pd.NA,
        "Dorchester",
    ]


def test_load_weather_units(tmp_path):
    path = tmp_path / "ghcnd.csv"
    path.write_text(
        '"STATION","DATE","PRCP","SNOW","TMAX"\n'
        '"X","2015-01-01","  254","   30","  322"\n'
        '"X","2015-01-02",,,"  -61"\n'
    )
    w = load_weather(path)
    assert w.loc["2015-01-01"].tolist() == [25.4, 30.0, 32.2]
    assert np.isnan(w.loc["2015-01-02", "prcp_mm"])
    assert w.loc["2015-01-02", "tmax_c"] == -6.1


def test_sparsity_counts_missing_days_as_empty():
    cells = pd.DataFrame({
        "neighborhood": ["A", "A", "B"],
        "date": pd.to_datetime(["2015-01-01", "2015-01-01", "2015-01-02"]),
        "storm_group": ["tree", "snow_ice", "tree"],
    })
    by_day = sparsity(cells, "2015-01-01", "2015-01-02", by_group=False)
    assert by_day == {"cells": 4, "non_empty": 2, "empty_pct": 50.0}
    by_group = sparsity(cells, "2015-01-01", "2015-01-02", by_group=True)
    assert by_group == {"cells": 8, "non_empty": 3, "empty_pct": 62.5}


def test_matched_ratio_skips_aftermath_controls():
    days = pd.date_range("2015-01-01", periods=10)
    counts = pd.Series([10, 30, 20, 20, 20, 10, 10, 10, 10, 10], index=days)
    event = pd.Series([False, True] + [False] * 8, index=days)
    control = ~event
    assert matched_ratio(counts, event, control) == 3.0
    assert matched_ratio(counts, event, control, lag=1) == 2.0


def test_matched_ratio_without_events_is_nan():
    days = pd.date_range("2015-01-01", periods=3)
    counts = pd.Series([1, 2, 3], index=days)
    none = pd.Series(False, index=days)
    assert np.isnan(matched_ratio(counts, none, ~none))
    assert np.isnan(matched_ratio(pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=bool)))


def test_legacy_storm_groups_keep_only_legacy_types_with_a_group(tmp_path):
    path = tmp_path / "category-map.csv"
    path.write_text(CATEGORY_MAP)
    assert legacy_storm_groups(path).to_dict("records") == [
        {"type": "Tree Emergencies", "storm_group": "tree"},
        {"type": "Request for Snow Plowing", "storm_group": "snow_ice"},
    ]


def test_inferred_format_loss_counts_the_times_pandas_cannot_read(tmp_path):
    # pandas infers one format from the first value, so a row in another format, a malformed
    # time and a blank one are all lost.
    path = tmp_path / "2015.csv"
    path.write_text("case_enquiry_id,open_dt\n1,2015-01-01 08:00:00\n2,2015-01-02 09:30:00\n"
                    "3,01/03/2015 10:00\n4,not a date\n5,\n")
    assert inferred_format_loss(path) == 3


def test_reconcile_compares_published_records_and_parsed_rows(tmp_path):
    (tmp_path / "2015.csv").write_text("case_enquiry_id,open_dt\n1,2015-01-01 08:00:00\n2,2015-01-02 09:00:00\n3,2015/01/03\n")
    (tmp_path / "new_system.csv").write_text("case_id,open_date\nA,2026-01-01\n")
    parsed = pd.DataFrame({"open_dt": pd.to_datetime(["2015-01-01 08:00", "2015-01-02 09:00"])})
    published = pd.DataFrame({"source": ["2015.csv", "new_system.csv"], "published_rows": [4, 1]})
    out = reconcile(tmp_path, parsed, published).set_index("source")
    columns = ["published", "csv_records", "inferred_format_lost", "parsed", "now_lost"]
    assert out.loc["2015.csv", columns].tolist() == [4, 3, 1, 2, 2]
    assert out.loc["2015.csv", "inferred_format_lost_pct"] == 25.0
    assert pd.isna(out.loc["new_system.csv", "parsed"])  # only the yearly files are parsed by year


def test_daily_cells_keep_storm_rows_with_a_neighborhood_on_their_local_day():
    storm_groups = pd.DataFrame({"type": ["Tree Emergencies"], "storm_group": ["tree"]})
    df = pd.DataFrame({
        "open_dt": pd.to_datetime(["2015-01-01 23:59:59", "2015-01-02 00:00:00", "2015-01-02 12:00:00", "2015-01-02 13:00:00"]),
        "type": ["Tree Emergencies", "Tree Emergencies", "Pothole Repair", "Tree Emergencies"],
        "neighborhood": ["Brighton", " Allston ", "Dorchester", "Boston"],
    })
    cells = daily_cells(df, storm_groups)
    # Midnight starts the next day; a type with no storm group and the citywide "Boston" are dropped.
    assert cells["date"].dt.strftime("%Y-%m-%d").tolist() == ["2015-01-01", "2015-01-02"]
    assert cells["neighborhood"].tolist() == ["Allston / Brighton", "Allston / Brighton"]


def test_sparsity_of_no_storm_rows_has_no_share():
    empty = pd.DataFrame({"neighborhood": pd.Series(dtype="string"), "date": pd.Series(dtype="datetime64[ns]"),
                          "storm_group": pd.Series(dtype="string")})
    result = sparsity(empty, "2015-01-01", "2015-01-02", by_group=True)
    assert result["cells"] == 0 and result["non_empty"] == 0
    assert np.isnan(result["empty_pct"])


def test_storm_ratios_count_the_end_day_to_its_last_second():
    days = pd.date_range("2015-01-01", "2015-01-31")
    weather = pd.DataFrame({"prcp_mm": 0.0, "snow_mm": 0.0, "tmax_c": 0.0}, index=days)
    weather.loc["2015-01-10", "snow_mm"] = 100.0
    storm_groups = pd.DataFrame({"type": ["Snow Plowing"], "storm_group": ["snow_ice"]})
    times = ["2015-01-05 12:00:00"] + ["2015-01-10 09:00:00"] * 4 + ["2015-01-31 23:59:59", "2015-02-01 00:00:00"]
    df = pd.DataFrame({"open_dt": pd.to_datetime(times), "type": "Snow Plowing"})
    snow = storm_ratios(df, storm_groups, weather, "2015-01-01", "2015-01-31").set_index("check").loc["snow_ice on snow days"]
    # Control days: no snow and not the 3 days after the snow day (27 days, 2 requests). The
    # 23:59:59 request counts on January 31; the one at midnight after it is outside the window.
    assert snow["event_days"] == 1
    assert snow["same_day"] == round(4 / (2 / 27), 2)
    assert snow["next_day"] == 0.0


def test_main_prints_reconciliation_sparsity_and_ratios(tmp_path, monkeypatch, capsys):
    pd.DataFrame({
        "open_dt": pd.to_datetime(["2015-01-05 08:00", "2015-01-10 09:00", "2024-01-10 09:00", "2024-03-01 10:00"]),
        "type": ["Tree Emergencies", "Request for Snow Plowing", "Tree Emergencies", "Pothole Repair"],
        "neighborhood": ["Brighton", "Dorchester", "Mattapan", "Boston"],
    }).to_parquet(tmp_path / "legacy_311.parquet", index=False)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "2015.csv").write_text("case_enquiry_id,open_dt\n1,2015-01-05 08:00:00\n2,2015-01-10 09:00:00\n")
    (raw / "2024.csv").write_text("case_enquiry_id,open_dt\n3,2024-01-10 09:00:00\n4,2024-03-01 10:00:00\n")
    (tmp_path / "published.csv").write_text("source,published_rows\n2015.csv,2\n2024.csv,2\n")
    (tmp_path / "map.csv").write_text(CATEGORY_MAP)
    weather = pd.DataFrame({"DATE": pd.date_range("2015-01-01", "2024-03-01").strftime("%Y-%m-%d"),
                            "PRCP": "0", "SNOW": "0", "TMAX": "100"})
    weather.loc[weather["DATE"] == "2015-01-10", "SNOW"] = "100"
    weather.to_csv(tmp_path / "ghcnd.csv", index=False)
    monkeypatch.setattr(sys, "argv", [
        "measure_311.py", "--parsed", str(tmp_path / "legacy_311.parquet"), "--raw-dir", str(raw),
        "--weather", str(tmp_path / "ghcnd.csv"), "--published", str(tmp_path / "published.csv"),
        "--map", str(tmp_path / "map.csv")])
    measure_311.main()
    out = capsys.readouterr().out
    for heading in ("## Reconciliation", "## Sparsity of storm-related cells", "## Storm ratios"):
        assert heading in out
    assert "| 2015 to 2026 | neighborhood x day x group |" in out
    assert "| snow_ice on snow days | 1 |" in out
