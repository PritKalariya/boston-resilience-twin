import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

import feature_table
from alert_history import OUT_COLUMNS as ALERT_COLUMNS
from alert_history import alert_feature_names
from daily_counts import STORM_GROUPS
from feature_table import (
    CALENDAR,
    KEYS,
    NO_VEHICLE,
    OLDER_ADULTS,
    POPULATION,
    POVERTY,
    STATIC_FEATURES,
    WINDOWS,
    build_features,
    check_no_leakage,
    feature_names,
    lag_features,
    static_attributes,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"
GROUPS = ["snow_ice", "tree"]
LAG = 2


def counts_table(units=("East", "West"), days=120, start="2016-01-01"):
    """Zero-filled daily counts where tree on day i has i requests, snow_ice 2i and flooding 100."""
    dates = pd.date_range(start, periods=days, freq="D")
    index = pd.MultiIndex.from_product([list(units), dates, STORM_GROUPS], names=["unit", "date", "storm_group"])
    df = index.to_frame(index=False)
    day = (df["date"] - pd.Timestamp(start)).dt.days
    per_group = {"tree": day, "snow_ice": 2 * day, "flooding": pd.Series(100, index=df.index)}
    df["requests"] = 0
    for group, values in per_group.items():
        df.loc[df["storm_group"] == group, "requests"] = values[df["storm_group"] == group]
    df["requests"] = df["requests"].astype("int64")
    return df.assign(baseline_requests=df["requests"])


def alerts_table(*alerts):
    """Prepared alerts like alert_history.py writes: (storm_groups, significance, first bulletin, first end) in UTC."""
    rows = [{
        "ugc": "MAZ015", "area": "zone", "phenomena": "WS", "significance": significance, "alert_type": "test",
        "storm_groups": groups, "eventid": i, "vtec_year": 2016, "product_id": f"p{i}",
        "first_bulletin_utc": pd.Timestamp(first, tz="UTC"), "first_end_utc": pd.Timestamp(end, tz="UTC"),
    } for i, (groups, significance, first, end) in enumerate(alerts)]
    return pd.DataFrame(rows, columns=ALERT_COLUMNS)


def make_static_dir(path, units=("East", "West"), population_units=None):
    """Two 0.01-degree squares side by side; the flood zones cover the west half of East."""
    path.mkdir(parents=True, exist_ok=True)
    shapes = [box(-71.10, 42.30, -71.09, 42.31), box(-71.09, 42.30, -71.08, 42.31)]
    gpd.GeoDataFrame({"neighborhood": list(units), "geometry": shapes[: len(units)]}, crs="EPSG:4326").to_parquet(
        path / "neighborhoods_by_tract20.parquet")
    flood = gpd.GeoDataFrame({"FID": [1], "geometry": [box(-71.10, 42.30, -71.095, 42.31)]}, crs="EPSG:4326")
    flood.to_parquet(path / "crb_flood_9in_10pct.parquet")
    flood.to_parquet(path / "crb_flood_9in_1pct.parquet")
    names = list(population_units or units)
    values = {"East": (1.0, 1000, 100, 200, 1000, 50, 400), "West": (2.0, 4000, 400, 400, 4000, 100, 1000)}
    population = pd.DataFrame({"name": names})
    population["arealand_sqmi"] = [values[n][0] for n in names]
    population[POPULATION] = [values[n][1] for n in names]
    for i, column in enumerate(OLDER_ADULTS):
        population[column] = [values[n][2] if i == 0 else 0 for n in names]
    population[POVERTY[0]], population[POVERTY[1]] = [values[n][3] for n in names], [values[n][4] for n in names]
    population[NO_VEHICLE[0]], population[NO_VEHICLE[1]] = [values[n][5] for n in names], [values[n][6] for n in names]
    population.to_parquet(path / "population_2025_neighborhood.parquet", index=False)
    return path


def row(table, unit, date):
    hit = table[(table["unit"] == unit) & (table["date"] == pd.Timestamp(date))]
    assert len(hit) == 1
    return hit.iloc[0]


# 311 lags


def test_lags_use_only_days_up_to_d_minus_the_lag():
    table = build_features(counts_table(), GROUPS, LAG)
    d = row(table, "East", "2016-01-21")  # day index 20
    assert d["tree_last"] == 18  # day 18 = D - 2
    assert d["tree_sum3"] == 16 + 17 + 18
    assert d["tree_sum7"] == sum(range(12, 19))
    assert d["snow_ice_last"] == 36
    assert np.isnan(d["tree_sum28"])  # needs days -9 to 18; the table starts at day 0


def test_changing_the_day_before_or_later_does_not_change_the_features():
    counts = counts_table()
    target = pd.Timestamp("2016-02-15")
    before = row(build_features(counts, GROUPS, LAG), "East", target)
    changed = counts.copy()
    changed.loc[changed["date"] >= target - pd.Timedelta(days=LAG - 1), ["requests", "baseline_requests"]] = 999
    after = row(build_features(changed, GROUPS, LAG), "East", target)
    pd.testing.assert_series_equal(before, after)
    moved = counts.copy()
    moved.loc[moved["date"] == target - pd.Timedelta(days=LAG), ["requests", "baseline_requests"]] = 999
    assert row(build_features(moved, GROUPS, LAG), "East", target)["tree_last"] == 999


def test_every_row_records_the_last_day_it_uses():
    table = build_features(counts_table(), GROUPS, LAG)
    assert ((table["date"] - table["features_last_day"]).dt.days == LAG).all()
    leaky = table.assign(features_last_day=table["date"] - pd.Timedelta(days=1))
    with pytest.raises(AssertionError, match="reach past"):
        check_no_leakage(leaky, LAG)


def test_a_lag_below_one_is_refused():
    with pytest.raises(ValueError, match="1 or more"):
        lag_features(counts_table(), GROUPS, 0)


def test_flooding_has_no_311_features_with_the_repo_settings():
    settings = feature_table.load_settings(CONFIG / "labels_features.csv")
    table = build_features(counts_table(), settings["feature_groups"], settings["feature_min_lag_days"])
    assert not any(column.startswith("flooding") for column in table.columns)
    assert set(feature_names(settings["feature_groups"])) <= set(table.columns)


def test_calendar_fields():
    d = row(build_features(counts_table(), GROUPS, LAG), "West", "2016-03-01")
    assert (d["day_of_week"], d["month"], d["day_of_year"]) == (1, 3, 61)  # a Tuesday in a leap year


def test_columns_and_one_row_per_unit_and_day():
    table = build_features(counts_table(days=50), GROUPS, LAG)
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS)]
    assert len(table) == 2 * 50 and not table.duplicated(["unit", "date"]).any()
    assert len(feature_names(GROUPS)) == len(GROUPS) * (1 + len(WINDOWS))


def test_a_date_range_keeps_the_earlier_history():
    counts = counts_table()
    full = build_features(counts, GROUPS, LAG)
    part = build_features(counts, GROUPS, LAG, start="2016-03-01", end="2016-03-31")
    assert len(part) == 2 * 31
    expected = full[(full["date"] >= "2016-03-01") & (full["date"] <= "2016-03-31")].reset_index(drop=True)
    pd.testing.assert_frame_equal(part, expected)
    assert part[feature_names(GROUPS)].notna().all().all()


# Forecast cutoff and alerts


def test_the_cutoff_is_6_pm_boston_time_the_evening_before():
    table = build_features(counts_table(), GROUPS, LAG)
    assert row(table, "East", "2016-01-15")["forecast_cutoff_utc"] == pd.Timestamp("2016-01-14 23:00", tz="UTC")
    assert row(table, "East", "2016-04-15")["forecast_cutoff_utc"] == pd.Timestamp("2016-04-14 22:00", tz="UTC")
    late = table.assign(forecast_cutoff_utc=table["forecast_cutoff_utc"] + pd.Timedelta(hours=7))
    with pytest.raises(AssertionError, match="cutoff"):
        check_no_leakage(late, LAG)


def test_alert_features_follow_the_cutoff_and_are_the_same_for_every_unit():
    # 2016-02-14 18:00 Boston (EST) is 23:00 UTC.
    alerts = alerts_table(("snow_ice", "W", "2016-02-14 22:59", "2016-02-17 12:00"),
                          ("tree", "Y", "2016-02-14 23:01", "2016-02-17 12:00"))
    table = build_features(counts_table(), GROUPS, LAG, alerts=alerts)
    feb15 = row(table, "East", "2016-02-15")
    assert (feb15["alert_snow_ice_count"], feb15["alert_snow_ice_warning"], feb15["alert_tree_count"]) == (1, 1, 0)
    assert row(table, "East", "2016-02-16")["alert_tree_count"] == 1
    on_day = table[table["date"] == "2016-02-15"][alert_feature_names(STORM_GROUPS)]
    assert len(on_day.drop_duplicates()) == 1


def test_full_table_has_every_feature_group(tmp_path):
    static = static_attributes(make_static_dir(tmp_path / "static"))
    alerts = alerts_table(("flooding", "W", "2016-02-14 12:00", "2016-02-16 00:00"))
    table = build_features(counts_table(), GROUPS, LAG, alerts=alerts, static=static)
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS), *alert_feature_names(STORM_GROUPS),
                                   *STATIC_FEATURES]
    assert len(table) == 2 * 120 and not table.duplicated(["unit", "date"]).any()
    assert table.groupby("unit")[STATIC_FEATURES].nunique().max().max() == 1


# Static attributes


def test_static_attributes_math(tmp_path):
    static = static_attributes(make_static_dir(tmp_path / "static")).set_index("unit")
    assert static.loc["East", "static_flood_10pct_share"] == pytest.approx(0.5, abs=0.01)
    assert static.loc["West", "static_flood_1pct_share"] == 0
    assert (static.loc["East", "static_population"], static.loc["West", "static_density"]) == (1000, 2000)
    assert static.loc["East", "static_poverty_share"] == 0.2
    assert static.loc["West", "static_older_adult_share"] == 0.1
    assert static.loc["East", "static_no_vehicle_share"] == 0.125


def test_static_attributes_need_every_neighborhood_and_column(tmp_path):
    with pytest.raises(ValueError, match="no 2025 population row"):
        static_attributes(make_static_dir(tmp_path / "a", population_units=["East"]))
    path = make_static_dir(tmp_path / "b")
    pd.read_parquet(path / "population_2025_neighborhood.parquet").drop(columns=POVERTY[0]).to_parquet(
        path / "population_2025_neighborhood.parquet", index=False)
    with pytest.raises(ValueError, match="missing columns"):
        static_attributes(path)


def test_every_unit_needs_static_attributes(tmp_path):
    static = static_attributes(make_static_dir(tmp_path / "static", units=("East",)))
    with pytest.raises(ValueError, match="no static attributes"):
        build_features(counts_table(), GROUPS, LAG, static=static)


# Empty, missing and malformed counts


def test_empty_counts_give_an_empty_table(tmp_path):
    static = static_attributes(make_static_dir(tmp_path / "static"))
    alerts = alerts_table(("snow_ice", "W", "2016-02-14 12:00", "2016-02-16 00:00"))
    table = build_features(counts_table().iloc[0:0], GROUPS, LAG, alerts=alerts, static=static)
    assert table.empty
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS), *alert_feature_names(STORM_GROUPS),
                                   *STATIC_FEATURES]


def test_missing_and_malformed_counts_are_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        build_features(counts_table().drop(columns="requests"), GROUPS, LAG)
    broken = counts_table()
    broken = pd.concat([broken, broken.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="repeat"):
        build_features(broken, GROUPS, LAG)


# Command line


def test_main_writes_the_full_table(tmp_path, monkeypatch, capsys):
    counts_table().to_parquet(tmp_path / "counts.parquet", index=False)
    alerts_table(("tree", "W", "2016-02-14 12:00", "2016-02-16 00:00")).to_parquet(tmp_path / "alerts.parquet", index=False)
    static_dir = make_static_dir(tmp_path / "static")
    argv = ["feature_table.py", "--counts", str(tmp_path / "counts.parquet"), "--alerts", str(tmp_path / "alerts.parquet"),
            "--static-dir", str(static_dir), "--settings", str(CONFIG / "labels_features.csv"),
            "--out", str(tmp_path / "features.parquet")]
    monkeypatch.setattr(sys, "argv", argv)
    feature_table.main()
    written = pd.read_parquet(tmp_path / "features.parquet")
    assert len(written) == 2 * 120
    assert set(alert_feature_names(STORM_GROUPS)) <= set(written.columns)
    assert set(STATIC_FEATURES) <= set(written.columns)
    printed = capsys.readouterr().out
    assert "D - 2" in printed and "18:00" in printed and "not yet in the table" in printed
