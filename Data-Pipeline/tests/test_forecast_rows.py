"""forecast_rows.py (#160) on files the downloads write with fake API answers (test_feature_sources)."""

import hashlib
import sys
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import feature_table
import forecast_rows
import noaa_logan
import open_meteo
import open_meteo_forecast as om
import tides
from surge_labels import load_settings
from test_feature_sources import CONFIG, PreviousRuns, store_logan, store_tides
from test_feature_table import alerts_table, counts_table, make_static_dir

AS_OF = date(2024, 2, 2)  # D = 2024-02-03
EVENING = datetime(2024, 2, 2, 23, 30, tzinfo=timezone.utc)  # 18:30 in Boston, after the cutoff


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Counts through as-of - 1, alerts, static layers, and the weather, tide and GFS downloads of the evening."""
    monkeypatch.setattr(noaa_logan, "FIRST_DAY", date(2023, 1, 1))
    monkeypatch.setattr(tides, "FIRST_DAY", date(2023, 7, 1))
    counts_table(start="2023-12-20", days=44).to_parquet(tmp_path / "counts.parquet", index=False)  # to 2024-02-01
    alerts_table(("snow_ice", "W", "2024-02-02 15:00", "2024-02-04 12:00")).to_parquet(tmp_path / "alerts.parquet",
                                                                                        index=False)
    store_logan(tmp_path, AS_OF, lambda datatype, day: 1.0)
    store_tides(tmp_path, AS_OF, lambda hour: 2.0)
    return tmp_path


def store_gfs(root, now, as_of=AS_OF):
    sets = open_meteo.read_config(CONFIG / "open_meteo.csv", today=as_of)
    run = open_meteo.run_download(sets, root / "raw" / "open_meteo", PreviousRuns(lambda name, hour: 1.0),
                                  state_root=root / "state" / "open_meteo", now=now, only=[om.SET], as_of=as_of,
                                  evening=True)
    assert not run["failed"], run


def run_main(root, monkeypatch, as_of=AS_OF, counts="counts.parquet"):
    out = root / "forecast" / str(as_of) / "features.parquet"
    monkeypatch.setattr(sys, "argv", [
        "forecast_rows.py", "--counts", str(root / counts), "--alerts", str(root / "alerts.parquet"),
        "--static-dir", str(make_static_dir(root / "static")), "--settings", str(CONFIG / "labels_features.csv"),
        "--noaa-dir", str(root / "raw" / "weather" / "noaa_logan"),
        "--forecast-dir", str(root / "raw" / "open_meteo" / om.SET),
        "--forecast-state", str(root / "state" / "open_meteo" / om.SET),
        "--tides-dir", str(root / "raw" / "tides" / "predicted"), "--as-of", str(as_of), "--out", str(out)])
    forecast_rows.main()
    return pd.read_parquet(out)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_tomorrows_rows_use_days_up_to_d_minus_2_and_leave_the_processed_files_alone(sources, monkeypatch):
    store_gfs(sources, EVENING)
    processed = sources / "processed"
    processed.mkdir()
    for name in ("labels.parquet", "features.parquet"):  # stand-ins: only their bytes matter here
        counts_table(start="2023-12-20", days=44).to_parquet(processed / name, index=False)
    before = {path.name: digest(path) for path in [sources / "counts.parquet", *processed.iterdir()]}

    rows = run_main(sources, monkeypatch)

    assert {path.name: digest(path) for path in [sources / "counts.parquet", *processed.iterdir()]} == before
    assert list(rows["unit"]) == ["East", "West"] and set(rows["date"]) == {pd.Timestamp("2024-02-03")}
    assert set(rows["features_last_day"]) == {pd.Timestamp("2024-02-01")}
    assert list(rows["tree_last"]) == [43, 43]  # day index 43 of the counts, 2024-02-01: real counts, not zeros
    assert (rows["wx_source"] == "forecast").all() and rows["wx_tmax_c"].notna().all()
    assert (rows["wx_known_by_utc"] <= rows["forecast_cutoff_utc"]).all()
    assert (rows["tide_pred_hours"] == 24).all() and rows["tide_pred_hours"].dtype == "int64"
    assert (rows["alert_snow_ice_count"] == 1).all()  # first sent before the cutoff, in effect on D
    catalog = pd.read_csv(CONFIG / "feature_catalog.csv", dtype=str, keep_default_na=False)
    assert sorted(rows.columns) == sorted(catalog.loc[catalog["use_in_model"] != "excluded", "feature"])


def test_rows_for_a_day_in_the_history_equal_the_feature_table(sources, monkeypatch):
    as_of = date(2024, 1, 30)  # D = 2024-01-31, inside the counts
    store_gfs(sources, datetime(2024, 1, 30, 23, 30, tzinfo=timezone.utc), as_of)
    rows = run_main(sources, monkeypatch, as_of)
    settings = load_settings(CONFIG / "labels_features.csv")
    counts, cutoff = pd.read_parquet(sources / "counts.parquet"), settings["forecast_cutoff_local"]
    days = feature_table.written_days(counts)  # every day, as feature_table.main builds them
    history = feature_table.build_features(
        counts, settings["feature_groups"], settings["feature_min_lag_days"],
        alerts=pd.read_parquet(sources / "alerts.parquet"), cutoff_local=cutoff,
        static=feature_table.static_attributes(sources / "static"),
        weather=feature_table.weather_features(days, cutoff, sources / "raw" / "weather" / "noaa_logan",
                                               sources / "raw" / "open_meteo" / om.SET,
                                               settings["observed_weather_lag_days"],
                                               settings["forecast_issue_margin_hours"], as_of),
        tides=feature_table.tide_features(days, sources / "raw" / "tides" / "predicted", as_of))
    history = history[history["date"] == pd.Timestamp("2024-01-31")].reset_index(drop=True)
    pd.testing.assert_frame_equal(rows, history)


def test_counts_that_end_before_d_minus_2_stop_the_run(sources, monkeypatch):
    store_gfs(sources, EVENING)
    counts_table(start="2023-12-20", days=43).to_parquet(sources / "short.parquet", index=False)  # to 2024-01-31
    with pytest.raises(ValueError, match="daily counts end on 2024-01-31, but the features of 2024-02-03 read 311 days "
                                         "up to 2024-02-01"):
        run_main(sources, monkeypatch, counts="short.parquet")


def test_a_forecast_fetched_before_the_cutoff_stops_the_run(sources, monkeypatch):
    store_gfs(sources, datetime(2024, 2, 2, 20, 0, tzinfo=timezone.utc))  # 15:00 in Boston
    with pytest.raises(ValueError, match="fetched 2024-02-02 20:00:00\\+00:00, before the cutoff 2024-02-02 23:00"):
        run_main(sources, monkeypatch)


def test_zero_days_are_added_only_after_the_last_counted_day():
    counts = counts_table(start="2024-01-01", days=10)  # to 2024-01-10
    extended = forecast_rows.extend_counts(counts, pd.Timestamp("2024-01-12"), 2)
    added = extended[extended["date"] > pd.Timestamp("2024-01-10")]
    assert sorted(added["date"].unique()) == [pd.Timestamp("2024-01-11"), pd.Timestamp("2024-01-12")]
    assert len(added) == 2 * 2 * 3 and (added[["requests", "baseline_requests"]] == 0).all().all()
    pd.testing.assert_frame_equal(extended.iloc[:len(counts)], counts)
    assert (extended.dtypes == counts.dtypes).all()
    assert forecast_rows.extend_counts(counts, pd.Timestamp("2024-01-10"), 0) is counts
