"""forecast_rows.py (#160) on files the downloads write with fake API answers (test_feature_sources)."""

import hashlib
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
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
CONTRACT_ROWS = Path(__file__).resolve().parents[2] / "docs/contracts/fixtures/forecast-rows/2026-11-25/features.parquet"
INPUTS = forecast_rows.model_inputs(CONFIG / "feature_catalog.csv")


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
    make_static_dir(tmp_path / "static")
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
        "--static-dir", str(root / "static"), "--settings", str(CONFIG / "labels_features.csv"),
        "--noaa-dir", str(root / "raw" / "weather" / "noaa_logan"),
        "--forecast-dir", str(root / "raw" / "open_meteo" / om.SET),
        "--forecast-state", str(root / "state" / "open_meteo" / om.SET),
        "--tides-dir", str(root / "raw" / "tides" / "predicted"), "--catalog", str(CONFIG / "feature_catalog.csv"),
        "--as-of", str(as_of), "--out", str(out)])
    forecast_rows.main()
    return pq.read_table(out)


def full_rows(root, as_of=AS_OF):
    """build_features' rows behind the file, with every column, as forecast_rows.forecast_rows returns them."""
    return forecast_rows.forecast_rows(
        pd.read_parquet(root / "counts.parquet"), pd.read_parquet(root / "alerts.parquet"),
        feature_table.static_attributes(root / "static"), load_settings(CONFIG / "labels_features.csv"),
        root / "raw" / "weather" / "noaa_logan", root / "raw" / "open_meteo" / om.SET,
        root / "state" / "open_meteo" / om.SET, root / "raw" / "tides" / "predicted", as_of)


def digests(root):
    """Every file under root with its sha256."""
    return {path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_tomorrows_rows_use_days_up_to_d_minus_2_and_write_nothing_else(sources, monkeypatch):
    store_gfs(sources, EVENING)
    before = digests(sources)

    table = run_main(sources, monkeypatch)

    # The counts and every download are byte for byte as they were; the one new file is the output.
    after = digests(sources)
    assert {path: sha for path, sha in after.items() if path in before} == before
    assert set(after) - set(before) == {Path("forecast") / str(AS_OF) / "features.parquet"}
    # The file is the /predict request rows (docs/contracts/predict.md): the contract fixture's columns and types.
    assert table.schema.equals(pq.read_schema(CONTRACT_ROWS))
    assert table.column_names == ["unit", "forecast_date", "as_of", *INPUTS]
    rows = table.to_pylist()
    assert [r["unit"] for r in rows] == ["East", "West"]
    assert {(r["forecast_date"], r["as_of"]) for r in rows} == {(date(2024, 2, 3), AS_OF)}
    assert all(r["alert_snow_ice_count"] == 1 for r in rows)  # first sent before the cutoff, in effect on D
    full = full_rows(sources)
    assert set(full["features_last_day"]) == {pd.Timestamp("2024-02-01")}
    assert list(full["tree_last"]) == [43, 43]  # day index 43 of the counts, 2024-02-01: real counts, not zeros
    assert (full["wx_source"] == "forecast").all() and (full["wx_known_by_utc"] <= full["forecast_cutoff_utc"]).all()
    assert (full["tide_pred_hours"] == 24).all()


def test_rows_for_a_day_in_the_history_equal_the_feature_table(sources, monkeypatch):
    as_of = date(2024, 1, 30)  # D = 2024-01-31, inside the counts
    store_gfs(sources, datetime(2024, 1, 30, 23, 30, tzinfo=timezone.utc), as_of)
    rows = run_main(sources, monkeypatch, as_of).to_pandas()
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
    history = history[history["date"] == pd.Timestamp("2024-01-31")].sort_values("unit", ignore_index=True)
    # Values only: the types are the contract's (the first test), and this fixture's static columns are whole numbers.
    pd.testing.assert_frame_equal(rows[["unit", *INPUTS]], history[["unit", *INPUTS]], check_dtype=False)


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


def test_an_input_the_feature_table_lacks_stops_the_file():
    table = pd.DataFrame({"unit": ["East"], "day_of_week": [1]})
    with pytest.raises(ValueError, match="the feature table has no month, which the catalog marks use_in_model = yes"):
        forecast_rows.request_rows(table, ["day_of_week", "month"], AS_OF)


def test_a_missing_input_is_a_null_in_the_file():
    table = pd.DataFrame({"unit": ["West", "East"], "day_of_week": [1, 1], "tree_rel3": [float("nan"), 2.0]})
    rows = forecast_rows.request_rows(table, ["day_of_week", "tree_rel3"], AS_OF)
    assert rows.column("tree_rel3").null_count == 1 and rows.column("tree_rel3").to_pylist() == [2.0, None]
    assert rows.column("unit").to_pylist() == ["East", "West"]


def test_a_gfs_set_with_no_manifest_stops_the_run(sources, monkeypatch):
    store_gfs(sources, EVENING)
    (sources / "state" / "open_meteo" / om.SET / "_manifest.json").unlink()
    with pytest.raises(ValueError, match="_manifest.json not found; run ingest_open_meteo in the evening run"):
        run_main(sources, monkeypatch)


def test_a_missing_weather_value_of_tomorrow_stops_the_run(sources, monkeypatch):
    sets = open_meteo.read_config(CONFIG / "open_meteo.csv", today=AS_OF)

    def gap(name, hour):  # tomorrow's 12:00 UTC temperature is missing in both forecasts
        return None if name.startswith("temperature_2m") and hour == "2024-02-03T12:00" else 1.0

    run = open_meteo.run_download(sets, sources / "raw" / "open_meteo", PreviousRuns(gap),
                                  state_root=sources / "state" / "open_meteo", now=EVENING, only=[om.SET], as_of=AS_OF)
    assert not run["failed"], run
    with pytest.raises(ValueError, match="2024-02-03 has no wx_tmax_c, wx_tmin_c; check the evening's Open-Meteo"):
        run_main(sources, monkeypatch)


def test_a_row_count_other_than_one_per_unit_stops_the_run(sources, monkeypatch):
    store_gfs(sources, EVENING)
    real = forecast_rows.build_features
    monkeypatch.setattr(forecast_rows, "build_features", lambda *a, **k: real(*a, **k).head(1))
    with pytest.raises(AssertionError, match="1 rows for 2 units"):
        full_rows(sources)
