import numpy as np
import pandas as pd

from measure_311 import load_weather, matched_ratio, normalize_neighborhood, sparsity


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
