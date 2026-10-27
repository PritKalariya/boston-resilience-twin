"""#16's readers on the files #7's and #8's own download code writes.

The downloads run with fake API answers in each source's format (no network,
conftest.py), so a change in what #7 or #8 store shows up here.
"""

import json
import sys
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

import feature_table
import noaa_logan
import open_meteo
import open_meteo_forecast as om
import tides
from alert_history import cutoff_utc
from test_feature_table import alerts_table, counts_table, make_static_dir, row
from tide_features import tide_features
from weather_features import read_observed

CONFIG = Path(__file__).resolve().parents[1] / "config"
UNITS = {"precipitation": "mm", "rain": "mm", "showers": "mm", "snowfall": "cm", "temperature_2m": "°C",
         "wind_gusts_10m": "m/s"}  # as in the stored set c files


def query(url):
    """A request's query as {name: value}, or a list for a name given more than once."""
    pairs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return {name: values if len(values) > 1 else values[0] for name, values in pairs.items()}


def cdo(value):
    """A fake NOAA CDO API: every requested datatype on every day, paged as CDO pages."""
    def fetch(url):
        q = query(url)
        results = [{"date": f"{day:%Y-%m-%d}T00:00:00", "datatype": datatype, "station": noaa_logan.STATION,
                    "attributes": ",,W,2400", "value": value(datatype, day)}
                   for day in pd.date_range(q["startdate"], q["enddate"]) for datatype in q["datatypeid"]]
        offset, limit = int(q["offset"]), int(q["limit"])
        return json.dumps({"metadata": {"resultset": {"offset": offset, "count": len(results), "limit": limit}},
                           "results": results[offset - 1:offset - 1 + limit]}).encode()
    return fetch


def coops(level):
    """A fake CO-OPS API: hourly rows in GMT for the requested days, metres as text."""
    def fetch(url):
        q = query(url)
        hours = pd.date_range(pd.Timestamp(q["begin_date"]), pd.Timestamp(q["end_date"]) + pd.Timedelta(hours=23), freq="h")
        rows = [{"t": f"{hour:%Y-%m-%d %H:%M}", "v": f"{level(hour):.3f}"} for hour in hours]
        if q["product"] == "predictions":
            return json.dumps({"predictions": rows}).encode()
        return json.dumps({"data": [{**r, "s": "0.003", "f": "0,0,0,0", "q": "v"} for r in rows]}).encode()
    return fetch


class PreviousRuns:
    """A fake Previous Runs API: every requested hour and value, with the stored files' units."""

    def __init__(self, value):
        self.value = value

    def get(self, url):
        q = query(url)
        times = open_meteo.hours(date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"]))
        names = q["hourly"].split(",")
        return json.dumps({
            "latitude": 42.36, "longitude": -71.0, "elevation": 5.0, "utc_offset_seconds": 0,
            "timezone": "GMT", "timezone_abbreviation": "GMT", "generationtime_ms": 1.0,
            "hourly_units": {"time": "iso8601", **{n: UNITS[n.rsplit("_previous_day", 1)[0]] for n in names}},
            "hourly": {"time": times, **{n: [self.value(n, t) for t in times] for n in names}},
        }).encode()


def store_logan(root, today, value):
    raw, state = root / "raw" / "weather" / "noaa_logan", root / "state" / "weather" / "noaa_logan"
    noaa_logan.refresh(raw, state, cdo(value), today, datetime(today.year, today.month, today.day, 12, tzinfo=timezone.utc),
                       pause=lambda seconds: None)
    return raw


def store_tides(root, today, level):
    raw, state = root / "raw" / "tides", root / "state" / "tides"
    tides.refresh(raw, state, coops(level), today, datetime(today.year, today.month, today.day, 12, tzinfo=timezone.utc))
    return raw / "predicted"


def store_set_c(root, now, value):
    raw, state = root / "raw" / "open_meteo", root / "state" / "open_meteo"
    sets = open_meteo.read_config(CONFIG / "open_meteo.csv", today=now.date())
    run = open_meteo.run_download(sets, raw, PreviousRuns(value), state_root=state, now=now, only=[om.SET])
    assert not run["failed"], run
    return raw / om.SET


def test_the_weather_reader_reads_what_noaa_logan_stores(tmp_path):
    today = date(2016, 2, 10)
    raw = store_logan(tmp_path, today, lambda datatype, day: float(day.day) if datatype == "PRCP" else 1.0)
    assert sorted(p.name for p in raw.iterdir()) == ["2015.json", "2016.json"]  # its manifest is under state/
    observed = read_observed(raw, [2015, 2016], today)
    assert len(observed) == 365 + 41  # 2015 came in three CDO pages; 2016 stops at today
    assert observed.loc["2015-12-31", "prcp_mm"] == 31.0 and observed.loc["2016-02-10", "wsf5_ms"] == 1.0


def test_the_tide_reader_reads_what_tides_stores(tmp_path):
    today = date(2015, 8, 1)
    peak = pd.Timestamp("2015-03-08 05:00")  # midnight EST at the start of the spring-forward day
    folder = store_tides(tmp_path, today, lambda hour: 3.2 if hour == peak else 1.0)
    assert sorted(p.name for p in folder.iterdir()) == ["2015-H1.json", "2015-H2.json"]
    table = tide_features(pd.DatetimeIndex(["2015-03-07", "2015-03-08", "2015-07-31"]), folder, today).set_index("date")
    assert list(table["tide_pred_hours"]) == [24, 23, 24]
    assert list(table["tide_pred_max_m"]) == [1.0, 3.2, 1.0]


def test_the_forecast_reader_reads_what_open_meteo_stores(tmp_path):
    now = datetime(2024, 2, 1, 6, tzinfo=timezone.utc)
    folder = store_set_c(tmp_path, now, lambda name, hour: 0.5 if name.startswith("precipitation") else 1.0)
    assert sorted(p.name for p in folder.iterdir()) == ["2024.json"]  # the year file and nothing else
    days = pd.DatetimeIndex(["2024-01-20", "2024-01-21", "2024-01-30"])
    daily = om.daily_forecasts(om.read_hourly(folder, [2024], now.date()), days, cutoff_utc(days, "18:00"), 7).set_index("date")
    assert pd.isna(daily.loc["2024-01-20", "precip_mm"])  # set c starts on 2024-01-21
    assert (daily.loc["2024-01-21", "precip_mm"], daily.loc["2024-01-30", "gust_max_ms"]) == (12.0, 1.0)
    cutoffs, used = pd.Series(cutoff_utc(days, "18:00"), index=days), daily["known_by_utc"].notna()
    assert used.any() and (daily.loc[used, "known_by_utc"] <= cutoffs[used]).all()


def test_the_feature_table_joins_every_source_by_unit_and_date(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(noaa_logan, "FIRST_DAY", date(2023, 1, 1))  # two years of fake CDO answers, not ten
    monkeypatch.setattr(tides, "FIRST_DAY", date(2023, 7, 1))
    noaa = store_logan(tmp_path, date(2024, 2, 29), lambda datatype, day: 2.5 if datatype == "PRCP" else 1.0)
    peak = pd.Timestamp("2024-01-10 15:00")
    tide_dir = store_tides(tmp_path, date(2024, 2, 29), lambda hour: 3.5 if hour == peak else 1.0)
    forecast = store_set_c(tmp_path, datetime(2024, 3, 1, 6, tzinfo=timezone.utc),
                           lambda name, hour: 0.5 if name.startswith("precipitation") else 1.0)
    counts_table(start="2023-12-20", days=45).to_parquet(tmp_path / "counts.parquet", index=False)  # to 2024-02-02
    alerts_table(("snow_ice", "W", "2024-01-09 12:00", "2024-01-11 12:00")).to_parquet(tmp_path / "alerts.parquet", index=False)
    out = tmp_path / "features.parquet"
    argv = ["feature_table.py", "--counts", str(tmp_path / "counts.parquet"), "--alerts", str(tmp_path / "alerts.parquet"),
            "--static-dir", str(make_static_dir(tmp_path / "static")), "--settings", str(CONFIG / "labels_features.csv"),
            "--noaa-dir", str(noaa), "--forecast-dir", str(forecast), "--tides-dir", str(tide_dir), "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    feature_table.main()

    table = pd.read_parquet(out)
    assert len(table) == 2 * 45 and not table.duplicated(["unit", "date"]).any()
    catalog = pd.read_csv(CONFIG / "feature_catalog.csv", dtype=str, keep_default_na=False)
    assert sorted(table.columns) == sorted(catalog.loc[catalog["use_in_model"] != "excluded", "feature"])
    dec31, jan10, jan25 = (row(table, "East", day) for day in ("2023-12-31", "2024-01-10", "2024-01-25"))
    assert (dec31["wx_source"], dec31["wx_precip_mm"]) == ("observed", 2.5)  # Logan, as #7 stores it
    assert jan10["wx_source"] == "forecast" and pd.isna(jan10["wx_precip_mm"])  # before set c starts: missing
    assert (jan25["wx_precip_mm"], jan25["wx_gust_max_ms"]) == (12.0, 1.0)  # set c, as #8 stores it
    assert jan25["wx_known_by_utc"] <= jan25["forecast_cutoff_utc"]
    assert (jan10["tide_pred_max_m"], jan25["tide_pred_max_m"]) == (3.5, 1.0)  # the predictions #7 stores
    assert (jan10["alert_snow_ice_count"], jan10["alert_snow_ice_warning"]) == (1, 1)
    assert (jan25["tree_last"], jan25["static_population"]) == (34, 1000)  # day D-2 of the counts; East's population
    shared = ["wx_source", "wx_precip_mm", "wx_tmax_c", "tide_pred_max_m", "alert_snow_ice_count"]
    for day in ("2023-12-31", "2024-01-10", "2024-01-25"):  # one value per day for every neighborhood
        pd.testing.assert_series_equal(row(table, "West", day)[shared], row(table, "East", day)[shared], check_names=False)
    assert "forecast days missing, kept missing: wx_precip_mm 20" in capsys.readouterr().out
