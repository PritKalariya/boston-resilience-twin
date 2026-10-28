import json
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
    relative_by_date,
    relative_features,
    relative_names,
    static_attributes,
    written_days,
)
from open_meteo_forecast import DAYS_BEFORE, VALUES, columns
from weather_features import STATION, weather_columns

CONFIG = Path(__file__).resolve().parents[1] / "config"
CATALOG = CONFIG / "feature_catalog.csv"
GROUPS = ["snow_ice", "tree"]
LAG = 2
TIDE_COLUMNS = ["tide_pred_hours", "tide_pred_max_m"]


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
    """Prepared alerts like alert_history.py writes: (storm_groups, significance, first bulletin, first end
    [, cancelled or upgraded]) in UTC."""
    rows = [{
        "ugc": "MAZ015", "area": "zone", "phenomena": "WS", "significance": significance, "alert_type": "test",
        "storm_groups": groups, "eventid": i, "vtec_year": 2016, "product_id": f"p{i}",
        "first_bulletin_utc": pd.Timestamp(first, tz="UTC"), "first_end_utc": pd.Timestamp(end, tz="UTC"),
        "ended_early_utc": pd.Timestamp(early[0], tz="UTC") if early else pd.NaT,
    } for i, (groups, significance, first, end, *early) in enumerate(alerts)]
    df = pd.DataFrame(rows, columns=ALERT_COLUMNS)
    return df.assign(ended_early_utc=pd.to_datetime(df["ended_early_utc"], utc=True))


def weather_frame(days, source=None, lag=2):
    """One row per day like weather_features: observed to 2023, forecast after (or `source` everywhere)."""
    days = pd.DatetimeIndex(days)
    frame = pd.DataFrame({"date": days})
    frame["wx_source"] = np.where(days <= pd.Timestamp("2023-12-31"), "observed", "forecast") if source is None else source
    frame["wx_known_by_utc"] = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    for i, column in enumerate(weather_columns(lag)[2:]):
        frame[column] = float(i)
    return frame


def tide_frame(days):
    return pd.DataFrame({"date": pd.DatetimeIndex(days), "tide_pred_hours": 24, "tide_pred_max_m": 3.0})


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


def test_the_leakage_check_reads_the_counts_by_date():
    counts = counts_table()
    table = build_features(counts, GROUPS, LAG)
    check_no_leakage(table, LAG, counts, GROUPS)  # every 311 feature is the counts up to features_last_day
    window_into_d = table.assign(tree_sum3=table.groupby("unit")["tree_sum3"].shift(-LAG))  # the 3 days ending on D
    with pytest.raises(AssertionError, match="tree_sum3 is not the counts up to features_last_day"):
        check_no_leakage(window_into_d, LAG, counts, GROUPS)
    day_before = table.assign(snow_ice_last=table["snow_ice_last"] + 2)  # snow_ice of D - 1: 2 more requests each day
    with pytest.raises(AssertionError, match="snow_ice_last is not the counts"):
        check_no_leakage(day_before, LAG, counts, GROUPS)


def test_a_lag_off_by_one_day_stops_the_build_although_features_last_day_says_d_minus_2(monkeypatch):
    """The mistake the gap check alone cannot see: features_last_day is date - min_lag whatever the lags read."""
    real = feature_table.lag_features

    def off_by_one(counts, groups, min_lag):  # reads D - 1, records D - 2
        table = real(counts, groups, min_lag - 1)
        return table.assign(features_last_day=table["date"] - pd.Timedelta(days=min_lag))

    monkeypatch.setattr(feature_table, "lag_features", off_by_one)
    with pytest.raises(AssertionError, match="is not the counts up to features_last_day"):
        build_features(counts_table(), GROUPS, LAG)


def test_a_lag_below_one_is_refused():
    with pytest.raises(ValueError, match="1 or more"):
        lag_features(counts_table(), GROUPS, 0)
    with pytest.raises(ValueError, match="1 or more"):
        relative_features(counts_table(), GROUPS, 0)


# Self-relative 311 lags (#33, ADR-025)

LONG = {"days": 1400, "start": "2015-01-01"}  # three years of history before the checked days


def test_self_relative_lags_divide_by_the_neighborhoods_own_baseline():
    # tree on day i has i requests, so the ADR-005 mean of a day with all 3 years of history is i - 730.
    table = build_features(counts_table(**LONG), GROUPS, LAG)
    d = row(table, "East", pd.Timestamp("2015-01-01") + pd.Timedelta(days=1200))  # D - 2 is day 1198
    assert d["tree_rel_last"] == pytest.approx((1198 + 1) / (1198 - 730 + 1))
    assert d["tree_rel3"] == pytest.approx((3 * 1197 + 1) / (3 * (1197 - 730) + 1))
    assert d["snow_ice_rel_last"] == pytest.approx((2 * 1198 + 1) / (2 * (1198 - 730) + 1))


def test_self_relative_lags_are_missing_until_there_is_a_baseline():
    table = build_features(counts_table(**LONG), GROUPS, LAG)
    first_year = table[table["date"] < "2016-01-01"]
    assert first_year[relative_names(GROUPS)].isna().all().all()
    assert first_year["tree_sum3"].notna().any()  # the raw lags are there
    later = table[table["date"] >= "2016-03-01"]
    assert later[relative_names(GROUPS)].notna().all().all()


def test_a_neighborhood_that_reports_half_as_much_gets_about_the_same_self_relative_lags():
    # The reporting gap (#32): West files half of East's requests for the same storms.
    counts = counts_table(**LONG)
    west = counts["unit"] == "West"
    counts.loc[west, ["requests", "baseline_requests"]] = counts.loc[west, "requests"] // 2
    table = build_features(counts, GROUPS, LAG)
    day = pd.Timestamp("2015-01-01") + pd.Timedelta(days=1300)
    east, west = row(table, "East", day), row(table, "West", day)
    assert east["tree_sum7"] / west["tree_sum7"] == pytest.approx(2, rel=0.01)
    assert east["tree_rel7"] / west["tree_rel7"] == pytest.approx(1, rel=0.01)


def test_changing_the_day_before_or_later_does_not_change_the_self_relative_lags():
    counts = counts_table(**LONG)
    target = pd.Timestamp("2015-01-01") + pd.Timedelta(days=1200)
    before = row(build_features(counts, GROUPS, LAG), "East", target)[relative_names(GROUPS)]
    changed = counts.copy()
    changed.loc[changed["date"] >= target - pd.Timedelta(days=LAG - 1), ["requests", "baseline_requests"]] = 999
    after = row(build_features(changed, GROUPS, LAG), "East", target)[relative_names(GROUPS)]
    pd.testing.assert_series_equal(before, after)


def test_the_leakage_check_reads_the_self_relative_lags_by_date(monkeypatch):
    counts = counts_table(**LONG)
    table = build_features(counts, GROUPS, LAG)
    check_no_leakage(table, LAG, counts, GROUPS)
    expected = relative_by_date(counts, table, GROUPS)
    assert np.allclose(table["tree_rel7"], expected["tree_rel7"], equal_nan=True)
    window_into_d = table.assign(tree_rel3=table.groupby("unit")["tree_rel3"].shift(-LAG))
    with pytest.raises(AssertionError, match="tree_rel3 is not the counts and baselines up to features_last_day"):
        check_no_leakage(window_into_d, LAG, counts, GROUPS)

    real = feature_table.relative_features
    monkeypatch.setattr(feature_table, "relative_features", lambda c, g, m: real(c, g, m - 1))  # reads D - 1
    with pytest.raises(AssertionError, match="is not the counts and baselines"):
        build_features(counts, GROUPS, LAG)


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
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS), *relative_names(GROUPS)]
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


def test_an_alert_cancelled_before_the_cutoff_is_left_out_of_the_table():
    # Cancelled at 20:00 UTC on 14 February, before that evening's 23:00 UTC cutoff (ADR-014 A+).
    alerts = alerts_table(("snow_ice", "W", "2016-02-14 12:00", "2016-02-17 12:00", "2016-02-14 20:00"))
    table = build_features(counts_table(), GROUPS, LAG, alerts=alerts)
    assert row(table, "East", "2016-02-15")["alert_snow_ice_count"] == 0


def test_full_table_has_every_feature_group(tmp_path):
    static = static_attributes(make_static_dir(tmp_path / "static"))
    alerts = alerts_table(("flooding", "W", "2016-02-14 12:00", "2016-02-16 00:00"))
    table = build_features(counts_table(), GROUPS, LAG, alerts=alerts, static=static)
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS), *relative_names(GROUPS), *alert_feature_names(STORM_GROUPS),
                                   *STATIC_FEATURES]
    assert len(table) == 2 * 120 and not table.duplicated(["unit", "date"]).any()
    assert table.groupby("unit")[STATIC_FEATURES].nunique().max().max() == 1


# Weather and tides


def test_weather_and_tides_join_by_date_and_are_the_same_for_every_unit():
    counts = counts_table()
    days = written_days(counts)
    table = build_features(counts, GROUPS, LAG, weather=weather_frame(days), tides=tide_frame(days))
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS), *relative_names(GROUPS), *weather_columns(LAG), *TIDE_COLUMNS]
    on_day = table[table["date"] == "2016-02-15"][[*weather_columns(LAG), *TIDE_COLUMNS]]
    assert len(on_day) == 2 and len(on_day.drop_duplicates()) == 1


def test_observed_weather_of_its_own_day_is_refused_after_the_training_years():
    counts = counts_table(start="2023-12-01", days=60)
    days = written_days(counts)
    build_features(counts, GROUPS, LAG, weather=weather_frame(days))  # observed to 2023, forecast after
    with pytest.raises(AssertionError, match="observed weather of their own day"):
        build_features(counts, GROUPS, LAG, weather=weather_frame(days, source="observed"))


def test_a_forecast_that_became_available_after_the_cutoff_is_refused():
    counts = counts_table(start="2023-12-01", days=60)
    weather = weather_frame(written_days(counts))
    jan10 = weather["date"] == pd.Timestamp("2024-01-10")  # cutoff 2024-01-09 23:00 UTC
    weather.loc[jan10, "wx_known_by_utc"] = pd.Timestamp("2024-01-09 23:00", tz="UTC")
    build_features(counts, GROUPS, LAG, weather=weather)  # at the cutoff itself is fine
    weather.loc[jan10, "wx_known_by_utc"] = pd.Timestamp("2024-01-09 23:01", tz="UTC")
    with pytest.raises(AssertionError, match="after its cutoff"):
        build_features(counts, GROUPS, LAG, weather=weather)


def test_weather_or_tides_without_a_written_day_are_refused():
    counts = counts_table()
    days = written_days(counts)
    with pytest.raises(ValueError, match="weather has no row for 1 of the days"):
        build_features(counts, GROUPS, LAG, weather=weather_frame(days[1:]))
    with pytest.raises(ValueError, match="tides has no row"):
        build_features(counts, GROUPS, LAG, tides=tide_frame(days[:-1]))
    with pytest.raises(ValueError, match="two rows for a day"):
        build_features(counts, GROUPS, LAG, weather=pd.concat([weather_frame(days), weather_frame(days[:1])]))
    # Days before start are not written, so they need no weather.
    part = build_features(counts, GROUPS, LAG, start="2016-02-01", weather=weather_frame(days[days >= "2016-02-01"]))
    assert len(part) == 2 * (120 - 31)


def test_feature_catalog_matches_the_full_table(tmp_path):
    catalog = pd.read_csv(CATALOG, dtype=str, keep_default_na=False)
    assert list(catalog.columns) == ["feature", "group", "source", "coverage_start", "history_rule", "live_rule",
                                     "use_in_model", "note"]
    assert not catalog["feature"].duplicated().any() and (catalog[["feature", "note"]] != "").all().all()
    assert set(catalog["history_rule"]) <= {"pass", "fail", "n/a"}
    assert set(catalog["live_rule"]) <= {"pass", "fail", "depends", "n/a"}
    assert set(catalog["use_in_model"]) <= {"yes", "no", "excluded"}
    model = catalog[catalog["use_in_model"] == "yes"]
    assert not (model[["history_rule", "live_rule"]] == "fail").any().any()  # a model feature passes both rules

    settings = feature_table.load_settings(CONFIG / "labels_features.csv")
    counts = counts_table()
    days = written_days(counts)
    table = build_features(counts, settings["feature_groups"], settings["feature_min_lag_days"], alerts=alerts_table(),
                           static=static_attributes(make_static_dir(tmp_path / "static")),
                           weather=weather_frame(days, lag=settings["observed_weather_lag_days"]), tides=tide_frame(days))
    in_table = catalog.loc[catalog["use_in_model"] != "excluded", "feature"]
    assert sorted(table.columns) == sorted(in_table)
    assert not set(catalog.loc[catalog["use_in_model"] == "excluded", "feature"]) & set(table.columns)


def test_the_same_inputs_in_any_row_order_give_the_same_table(tmp_path):
    counts = counts_table()
    days = written_days(counts)
    alerts = alerts_table(("snow_ice", "W", "2016-02-14 12:00", "2016-02-17 12:00"),
                          ("tree", "Y", "2016-02-15 03:00", "2016-02-18 00:00", "2016-02-16 01:00"))
    static = static_attributes(make_static_dir(tmp_path / "static"))
    first = build_features(counts, GROUPS, LAG, alerts=alerts, static=static, weather=weather_frame(days), tides=tide_frame(days))
    again = build_features(counts.sample(frac=1, random_state=1), GROUPS, LAG, alerts=alerts.iloc[::-1],
                           static=static.iloc[::-1], weather=weather_frame(days).iloc[::-1],
                           tides=tide_frame(days).sample(frac=1, random_state=2))
    pd.testing.assert_frame_equal(first, again)


# Static attributes


def test_static_attributes_math(tmp_path):
    static = static_attributes(make_static_dir(tmp_path / "static")).set_index("unit")
    assert static.loc["East", "static_flood_10pct_share"] == pytest.approx(0.5, abs=0.01)
    assert static.loc["West", "static_flood_1pct_share"] == 0
    assert (static.loc["East", "static_population"], static.loc["West", "static_density"]) == (1000, 2000)
    assert static.loc["East", "static_poverty_share"] == 0.2
    assert static.loc["West", "static_older_adult_share"] == 0.1
    assert static.loc["East", "static_no_vehicle_share"] == 0.125


def test_a_missing_age_group_leaves_the_older_adult_share_missing(tmp_path):
    path = make_static_dir(tmp_path / "static")
    file = path / "population_2025_neighborhood.parquet"
    population = pd.read_parquet(file)
    population[OLDER_ADULTS[3]] = population[OLDER_ADULTS[3]].astype("float64")
    population.loc[population["name"] == "East", OLDER_ADULTS[3]] = np.nan  # one of the ten groups unknown
    population.to_parquet(file, index=False)
    static = static_attributes(path).set_index("unit")
    assert pd.isna(static.loc["East", "static_older_adult_share"])  # not the 0.1 the other nine give
    assert static.loc["West", "static_older_adult_share"] == 0.1


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
    assert list(table.columns) == [*KEYS, *CALENDAR, *feature_names(GROUPS), *relative_names(GROUPS), *alert_feature_names(STORM_GROUPS),
                                   *STATIC_FEATURES]


def test_missing_and_malformed_counts_are_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        build_features(counts_table().drop(columns="requests"), GROUPS, LAG)
    broken = counts_table()
    broken = pd.concat([broken, broken.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="repeat"):
        build_features(broken, GROUPS, LAG)


def test_days_the_counts_lack_are_refused_and_earlier_days_are_not_asked_for():
    counts = counts_table()  # 2016-01-01 to 2016-04-29
    with pytest.raises(ValueError, match="30 of the 30 days from 2016-06-01 to 2016-06-30 have no counts"):
        written_days(counts, "2016-06-01", "2016-06-30")
    with pytest.raises(ValueError, match=r"1 of the 30 days .*\(2016-04-30 to 2016-04-30\)"):
        written_days(counts, "2016-04-01", "2016-04-30")
    assert len(written_days(counts, "2016-03-01", "2016-03-31")) == 31  # its lags read February, not asked for
    assert len(written_days(counts)) == 120  # the full table: its first days have no lags, as before
    with pytest.raises(ValueError, match="no rows"):
        written_days(counts.iloc[0:0])


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


def test_main_stops_on_days_after_the_counts_and_writes_nothing(tmp_path, monkeypatch):
    counts_table().to_parquet(tmp_path / "counts.parquet", index=False)  # 2016-01-01 to 2016-04-29
    alerts_table().to_parquet(tmp_path / "alerts.parquet", index=False)
    out = tmp_path / "features.parquet"
    argv = ["feature_table.py", "--counts", str(tmp_path / "counts.parquet"), "--alerts", str(tmp_path / "alerts.parquet"),
            "--static-dir", str(make_static_dir(tmp_path / "static")), "--settings", str(CONFIG / "labels_features.csv"),
            "--out", str(out)]
    monkeypatch.setattr(sys, "argv", [*argv, "--start", "2016-04-15", "--end", "2016-05-14"])
    with pytest.raises(ValueError, match=r"15 of the 30 days .*\(2016-04-30 to 2016-05-14\)"):
        feature_table.main()
    assert not out.exists()
    monkeypatch.setattr(sys, "argv", [*argv, "--start", "2016-03-01", "--end", "2016-03-31"])
    feature_table.main()
    written = pd.read_parquet(out)
    assert len(written) == 2 * 31 and written[feature_names(GROUPS)].notna().all().all()  # lags reach into February


def write_json(folder, name, body):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(body))
    return folder


def raw_sources(tmp_path):
    """#7's Logan files (precipitation 2.5 mm every day), #8's set c from 2024-01-21 (0.5 mm every hour)
    and #7's tide predictions (1 m, with a 3.5 m peak at 15:00 UTC on 2024-01-10)."""
    noaa = tmp_path / "noaa_logan"
    for year, start, end in ((2023, "2023-12-01", "2023-12-31"), (2024, "2024-01-01", "2024-02-29")):
        results = [{"date": f"{day:%Y-%m-%d}T00:00:00", "datatype": datatype, "station": STATION,
                    "attributes": ",,W,2400", "value": 2.5}
                   for day in pd.date_range(start, end) for datatype in ("PRCP", "SNOW", "TMAX", "TMIN", "AWND", "WSF5")]
        write_json(noaa, year, {"metadata": {}, "results": results})
    hours = pd.date_range("2024-01-21 00:00", "2024-02-29 23:00", freq="h")
    hourly = {"time": [f"{hour:%Y-%m-%dT%H:%M}" for hour in hours],
              **{column: [0.5 if column.startswith("precipitation") else 1.0] * len(hours) for column in columns()}}
    units = {f"{name}_previous_day{n}": unit for name, unit, *_ in VALUES.values() for n in DAYS_BEFORE}
    forecast = write_json(tmp_path / "set_c", 2024, {"utc_offset_seconds": 0, "hourly_units": units, "hourly": hourly})
    peak = pd.Timestamp("2024-01-10 15:00")
    tides = tmp_path / "predicted"
    for name, start, end in (("2023-H2", "2023-12-01", "2023-12-31"), ("2024-H1", "2024-01-01", "2024-02-29")):
        rows = [{"t": f"{hour:%Y-%m-%d %H:%M}", "v": "3.500" if hour == peak else "1.000"}
                for hour in pd.date_range(f"{start} 00:00", f"{end} 23:00", freq="h")]
        write_json(tides, name, {"predictions": rows})
    return noaa, forecast, tides


def test_main_adds_weather_and_tides_from_the_raw_files(tmp_path, monkeypatch, capsys):
    counts_table(start="2023-12-20", days=45).to_parquet(tmp_path / "counts.parquet", index=False)  # to 2024-02-02
    alerts_table().to_parquet(tmp_path / "alerts.parquet", index=False)
    noaa, forecast, tides = raw_sources(tmp_path)
    out = tmp_path / "features.parquet"
    argv = ["feature_table.py", "--counts", str(tmp_path / "counts.parquet"), "--alerts", str(tmp_path / "alerts.parquet"),
            "--static-dir", str(make_static_dir(tmp_path / "static")), "--settings", str(CONFIG / "labels_features.csv"),
            "--noaa-dir", str(noaa), "--forecast-dir", str(forecast), "--tides-dir", str(tides), "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    feature_table.main()
    written = pd.read_parquet(out)
    assert len(written) == 2 * 45
    dec31, jan10, jan25 = (row(written, "East", day) for day in ("2023-12-31", "2024-01-10", "2024-01-25"))
    assert (dec31["wx_source"], dec31["wx_precip_mm"]) == ("observed", 2.5)
    assert jan10["wx_source"] == "forecast" and pd.isna(jan10["wx_precip_mm"])  # before set c: missing, not observed
    assert (jan25["wx_precip_mm"], jan25["wx_tmax_c"]) == (12.0, 1.0)
    assert (jan10["tide_pred_max_m"], jan25["tide_pred_max_m"], jan25["tide_pred_hours"]) == (3.5, 1.0, 24)
    printed = capsys.readouterr().out
    assert "forecast days missing, kept missing: wx_precip_mm 20" in printed and "not yet in the table" not in printed

    monkeypatch.setattr(sys, "argv", [arg for arg in argv if arg not in ("--forecast-dir", str(forecast))])
    with pytest.raises(SystemExit):
        feature_table.main()
