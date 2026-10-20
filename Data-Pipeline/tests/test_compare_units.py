import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from compare_units import STORM_GROUPS, any_group, group_metrics, locate, panel, tract_neighborhoods, unit_metrics

# Two tracts side by side in Boston, both in one neighborhood, plus a second
# neighborhood with no tract in it.
TRACTS = gpd.GeoDataFrame(
    {"geoid20": ["25025000100", "25025000200"]},
    geometry=[box(-71.10, 42.30, -71.08, 42.32), box(-71.08, 42.30, -71.06, 42.32)],
    crs="EPSG:4326",
)
HOODS = gpd.GeoDataFrame(
    {"neighborhood": ["Roxbury", "Fenway"]},
    geometry=[box(-71.10, 42.30, -71.06, 42.32), box(-71.20, 42.40, -71.18, 42.42)],
    crs="EPSG:4326",
)


def requests(rows):
    return pd.DataFrame(rows, columns=["open_dt", "storm_group", "latitude", "longitude"]).astype({"open_dt": "datetime64[ns]"})


def test_tracts_map_to_their_neighborhood():
    assert tract_neighborhoods(TRACTS, HOODS).to_dict() == {"25025000100": "Roxbury", "25025000200": "Roxbury"}


def test_tract_outside_every_neighborhood_is_kept_without_one():
    hoods = tract_neighborhoods(TRACTS, HOODS[HOODS["neighborhood"] == "Fenway"])
    assert hoods.isna().all()
    df = requests([("2024-01-01 08:00", "tree", 42.31, -71.09)])
    located, stats = locate(df, TRACTS, hoods)
    assert located["geoid20"].tolist() == ["25025000100"]
    assert stats["tract_without_neighborhood"] == 1


def test_locate_counts_what_cannot_be_placed():
    df = requests([
        ("2024-01-01 08:00", "tree", 42.31, -71.09),     # tract 100
        ("2024-01-01 09:00", "tree", 42.31, -71.07),     # tract 200
        ("2024-01-01 10:00", "tree", None, None),         # no coordinates
        ("2024-01-01 11:00", "tree", "north", -71.07),    # unparseable
        ("2024-01-01 13:00", "tree", 42.50, -71.50),      # outside Boston tracts
    ])
    located, stats = locate(df, TRACTS, tract_neighborhoods(TRACTS, HOODS))
    assert located["geoid20"].tolist() == ["25025000100", "25025000200"]
    assert located["neighborhood"].tolist() == ["Roxbury", "Roxbury"]
    assert stats == {"rows": 5, "no_coordinates": 2, "outside_tracts": 1, "located": 2, "tract_without_neighborhood": 0}


def test_locate_empty_input():
    located, stats = locate(requests([]), TRACTS, tract_neighborhoods(TRACTS, HOODS))
    assert located.empty
    assert stats["rows"] == 0 and stats["located"] == 0


def test_panel_fills_every_unit_group_and_day():
    df = requests([("2024-01-02 08:00", "tree", 42.31, -71.09)])
    located, _ = locate(df, TRACTS, tract_neighborhoods(TRACTS, HOODS))
    days = pd.date_range("2024-01-01", "2024-01-03")
    counts = panel(located, "geoid20", True, TRACTS["geoid20"], days)
    assert counts.shape == (3, 2 * len(STORM_GROUPS))
    assert counts.to_numpy().sum() == 1
    assert counts.loc["2024-01-02", ("25025000100", "tree")] == 1


def test_panel_with_no_requests_is_all_zero():
    days = pd.date_range("2024-01-01", "2024-01-03")
    counts = panel(requests([]).assign(neighborhood=pd.Series(dtype=str)), "neighborhood", False, ["Roxbury", "Fenway"], days)
    assert counts.shape == (3, 2)
    assert (counts == 0).all().all()


def test_panel_citywide_has_one_unit():
    df = requests([("2024-01-02 08:00", "snow_ice", 42.31, -71.09), ("2024-01-02 09:00", "snow_ice", 42.31, -71.07)])
    located, _ = locate(df, TRACTS, tract_neighborhoods(TRACTS, HOODS))
    counts = panel(located, "city", True, None, pd.date_range("2024-01-01", "2024-01-03"))
    assert list(counts.columns.get_level_values(0).unique()) == ["Boston"]
    assert counts.loc["2024-01-02", ("Boston", "snow_ice")] == 2


def daily(values_by_day, start="2020-01-01"):
    days = pd.date_range(start, periods=len(values_by_day))
    return pd.DataFrame({"a": values_by_day}, index=days)


def test_metrics_empty_share_and_spike():
    # Three quiet years of 1 or 0 a day, then a spike of 10 on one day.
    rng = np.random.default_rng(0)
    values = rng.integers(0, 2, size=365 * 4)
    values[365 * 3 + 100] = 10
    counts = daily(values)
    windows = {"all": ("2020-01-01", counts.index[-1]), "last year": ("2022-12-31", counts.index[-1])}
    rows = unit_metrics(counts, windows, grid=[(2.0, 3)])
    assert rows[0]["empty_pct"] == round(100 * float((values == 0).mean()), 1)
    assert rows[1]["surge_pct k2 m3"] > 0
    assert rows[1]["columns with no surge, k2 m3"] == 0


def test_metrics_first_year_has_no_baseline():
    counts = daily(np.full(300, 5))
    rows = unit_metrics(counts, {"all": ("2020-01-01", counts.index[-1])}, grid=[(2.0, 3)])
    assert rows[0]["surge_pct k2 m3"] == 0
    assert rows[0]["columns with no surge, k2 m3"] == 1


def test_group_metrics_one_row_per_group_and_window():
    days = pd.date_range("2020-01-01", periods=400)
    columns = pd.MultiIndex.from_product([["Roxbury"], STORM_GROUPS], names=["neighborhood", "storm_group"])
    counts = pd.DataFrame(0, index=days, columns=columns)
    rows = group_metrics(counts, {"all": (days[0], days[-1])})
    assert [r["storm_group"] for r in rows] == STORM_GROUPS
    assert all(r["empty_pct"] == 100.0 for r in rows)


def test_any_group_collapses_to_one_column_per_unit():
    days = pd.date_range("2024-01-01", periods=2)
    columns = pd.MultiIndex.from_product([["Roxbury", "Fenway"], STORM_GROUPS], names=["neighborhood", "storm_group"])
    flags = pd.DataFrame(False, index=days, columns=columns)
    flags.loc[days[1], ("Roxbury", "tree")] = True
    out = any_group(flags)
    assert sorted(out.columns) == ["Fenway", "Roxbury"]
    assert out["Roxbury"].tolist() == [False, True] and not out["Fenway"].any()


def test_any_group_metrics_count_unit_days_not_group_days():
    # One unit, three groups; tree spikes on one day in the fourth year.
    rng = np.random.default_rng(1)
    days = pd.date_range("2020-01-01", periods=365 * 4)
    columns = pd.MultiIndex.from_product([["Roxbury"], STORM_GROUPS], names=["neighborhood", "storm_group"])
    counts = pd.DataFrame(rng.integers(0, 2, size=(len(days), 3)), index=days, columns=columns)
    counts.iloc[365 * 3 + 50, 1] = 12
    windows = {"last year": (days[365 * 3], days[-1])}
    pooled_empty = (counts.loc[days[365 * 3]:].sum(axis=1) == 0).mean()
    row = unit_metrics(counts, windows, grid=[(2.0, 3)], collapse=True)[0]
    assert row["units"] == 1 and row["cells"] == 365
    assert row["empty_pct"] == round(100 * float(pooled_empty), 1)
    assert row["surge_pct k2 m3"] > 0
