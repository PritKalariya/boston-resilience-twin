import csv
import io
import json
import logging
import re
import shutil
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import open_meteo
from open_meteo import (
    ATTRIBUTION_COLUMNS,
    CONFIG_COLUMNS,
    attribution_rows,
    check_answer,
    hours,
    latest_full_day,
    read_config,
    request_url,
    run_download,
    year_chunks,
)

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)  # yesterday is 2026-10-21
REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "open_meteo.csv"
HEADER = ",".join(CONFIG_COLUMNS) + "\n"
HISTORY_ROW = (
    "history_era5,https://archive-api.open-meteo.com/v1/archive,era5,2024-11-01,latest_values,"
    "precipitation;rain;snowfall,Logan,42.36057,-71.00975,no,History,Copernicus {year},https://example.org/c\n"
)
FORECAST_ROW = (
    "first_hours_ecmwf_ifs,https://historical-forecast-api.open-meteo.com/v1/forecast,ecmwf_ifs,2024-11-01,"
    "yesterday,precipitation;rain;showers,Logan,42.36057,-71.00975,no,First hours,ECMWF,https://example.org/e\n"
)
DAY_BEFORE_ROW = (
    "day_before_ncep_gfs_global,https://previous-runs-api.open-meteo.com/v1/forecast,ncep_gfs_global,2024-11-15,"
    "yesterday,temperature_2m_previous_day1;temperature_2m_previous_day2;snowfall_previous_day1;snowfall_previous_day2,"
    "Logan,42.36057,-71.00975,yes,Day before,NOAA NCEP,https://example.org/n\n"
)
# GFS day-before snowfall (and rain) start partway through, after the other values.
SNOW_STARTS = {"snowfall_previous_day1": "2024-12-06T18:00", "snowfall_previous_day2": "2024-12-07T18:00"}


class FakeOpenMeteo:
    """Answers like the API: every hour from start_date to end_date, with data up to
    `data_until` per host (ERA5 runs behind), from `value_starts` per value, and none
    for the values in `without`."""

    def __init__(self, data_until=None, without=("showers",), value_starts=None):
        self.data_until = data_until or {"archive-api.open-meteo.com": "2026-10-16T23:00"}
        self.without = set(without)
        self.value_starts = value_starts or {}
        self.answer_with = None  # bytes or an exception to give instead of an answer
        self.urls = []
        self.bodies = {}

    def get(self, url):
        self.urls.append(url)
        if isinstance(self.answer_with, Exception):
            raise self.answer_with
        if self.answer_with is not None:
            return self.answer_with
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        until = self.data_until.get(parts.hostname, "9999-12-31T23:00")
        times = hours(date.fromisoformat(query["start_date"]), date.fromisoformat(query["end_date"]))
        values = query["hourly"].split(",")
        hourly = {"time": times}
        for value in values:
            starts = self.value_starts.get(value, "")
            hourly[value] = [None if value in self.without or t > until or t < starts else 0.5 for t in times]
        body = json.dumps({
            "latitude": 42.25,
            "longitude": -71.0,
            "elevation": 3.0,
            "utc_offset_seconds": 0,
            "timezone": "GMT",
            "hourly_units": {"time": "iso8601", **{v: "mm" for v in values}},
            "hourly": hourly,
        }).encode()
        self.bodies[url] = body
        return body


def write_config(tmp_path, *rows, header=HEADER):
    path = tmp_path / "open_meteo.csv"
    path.write_text(header + "".join(rows))
    return path


def csv_line(row):
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow([row[c] for c in CONFIG_COLUMNS])
    return buffer.getvalue()


def weather_sets(tmp_path):
    return read_config(write_config(tmp_path, HISTORY_ROW, FORECAST_ROW), today=NOW.date())


def run(tmp_path, client, now=NOW, **kwargs):
    return run_download(weather_sets(tmp_path), tmp_path / "raw", client, state_root=tmp_path / "state", now=now, **kwargs)


def year_files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.json")) if not p.name.startswith("_")}


def files_in(folder):
    """Every file under folder with its bytes and modification time."""
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.rglob("*")) if p.is_file()}


def manifest(tmp_path, name):
    return json.loads((tmp_path / "state" / name / "_manifest.json").read_text())


def rows_of(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main_args(tmp_path, *extra):
    config = write_config(tmp_path, HISTORY_ROW, FORECAST_ROW)
    return ["--config", str(config), "--raw-dir", str(tmp_path / "raw"), "--state-dir", str(tmp_path / "state"),
            "--attribution", str(tmp_path / "open-meteo.csv"), *extra]


def use(monkeypatch, client, now=NOW):
    """Run main() with this client and this clock."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(open_meteo, "OpenMeteoClient", lambda pause: client)
    monkeypatch.setattr(open_meteo, "datetime", Clock)


def answer(end=date(2025, 1, 2), **changes):
    """An answer for 2025-01-01 to `end` with every history value filled."""
    times = hours(date(2025, 1, 1), end)
    values = ("precipitation", "rain", "snowfall")
    data = {
        "latitude": 42.25,
        "longitude": -71.0,
        "elevation": 3.0,
        "utc_offset_seconds": 0,
        "hourly_units": {v: "mm" for v in values},
        "hourly": {"time": times, **{v: [0.5] * len(times) for v in values}},
    }
    data.update(changes)
    return data


def check(tmp_path, data):
    history = weather_sets(tmp_path)[0]
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    return check_answer(body, history, date(2025, 1, 1), date(2025, 1, 2))


# Config

def test_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_config(tmp_path / "absent.csv")


def test_config_empty_file(tmp_path):
    with pytest.raises(ValueError, match="is empty"):
        read_config(write_config(tmp_path, header=""))


def test_config_header_only(tmp_path):
    with pytest.raises(ValueError, match="lists no sets"):
        read_config(write_config(tmp_path))


def test_config_missing_column(tmp_path):
    with pytest.raises(ValueError, match="missing columns"):
        read_config(write_config(tmp_path, "history_era5,https://archive-api.open-meteo.com/v1/archive\n", header="name,url\n"))


def test_config_row_with_extra_fields(tmp_path):
    with pytest.raises(ValueError, match="more fields than the header"):
        read_config(write_config(tmp_path, HISTORY_ROW.rstrip("\n") + ",extra\n"), today=NOW.date())


@pytest.mark.parametrize(
    "column, value, message",
    [
        ("name", "History ERA5", "set name"),
        ("url", "http://archive-api.open-meteo.com/v1/archive", "https Open-Meteo"),
        ("url", "https://example.org/v1/archive", "https Open-Meteo"),
        ("model", "ERA5", "model name"),
        ("start", "2021-13-01", "not a date"),
        ("start", "2026-10-22", "not before today"),
        ("end", "today", "expected one of"),
        ("hourly", "precipitation;Rain", "value names"),
        ("hourly", "rain;rain", "repeats a value"),
        ("latitude", "north", "must be numbers"),
        ("longitude", "-200", "not a place on Earth"),
        ("validation_test", "maybe", "yes or no"),
        ("credit", "", "credit is blank"),
    ],
)
def test_config_bad_value(tmp_path, column, value, message):
    row = dict(zip(CONFIG_COLUMNS, next(csv.reader([HISTORY_ROW]))))
    row[column] = value
    with pytest.raises(ValueError, match=message):
        read_config(write_config(tmp_path, csv_line(row)), today=NOW.date())


def test_config_repeated_name(tmp_path):
    with pytest.raises(ValueError, match="repeats set names"):
        read_config(write_config(tmp_path, HISTORY_ROW, HISTORY_ROW), today=NOW.date())


def test_repo_config():
    sets = {s.name: s for s in read_config(REPO_CONFIG)}
    assert set(sets) == {"history_era5", "first_hours_ecmwf_ifs", "day_before_ncep_gfs_global"}
    history, first_hours = sets["history_era5"], sets["first_hours_ecmwf_ifs"]
    day_before = sets["day_before_ncep_gfs_global"]
    assert (history.model, history.start, history.end) == ("era5", date(2021, 1, 1), "latest_values")
    assert (first_hours.model, first_hours.start, first_hours.end) == ("ecmwf_ifs", date(2021, 1, 1), "yesterday")
    assert (day_before.model, day_before.start, day_before.end) == ("ncep_gfs_global", date(2024, 1, 21), "yesterday")
    assert "showers" not in history.hourly  # the history API has no showers value
    assert "showers" in first_hours.hourly
    values = ("precipitation", "rain", "showers", "snowfall", "temperature_2m", "wind_gusts_10m")
    assert set(day_before.hourly) == {f"{v}_previous_day{n}" for v in values for n in (1, 2)}
    # Only the day-before forecasts were known in advance, so only they may be used in validation and test.
    assert {name: s.validation_test for name, s in sets.items()} == {
        "history_era5": "no", "first_hours_ecmwf_ifs": "no", "day_before_ncep_gfs_global": "yes",
    }


# Planning

def test_year_chunks():
    assert year_chunks(date(2024, 11, 1), date(2026, 10, 21)) == [
        (date(2024, 11, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 10, 21)),
    ]
    assert year_chunks(date(2026, 3, 1), date(2026, 2, 28)) == []


def test_request_asks_for_hourly_utc_values_with_gusts_in_ms(tmp_path):
    history = weather_sets(tmp_path)[0]
    url = request_url(history, date(2025, 1, 1), date(2025, 1, 7))
    assert url.startswith("https://archive-api.open-meteo.com/v1/archive?")
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)) == {
        "latitude": "42.36057",
        "longitude": "-71.00975",
        "start_date": "2025-01-01",
        "end_date": "2025-01-07",
        "hourly": "precipitation,rain,snowfall",
        "models": "era5",
        "timezone": "GMT",
        "wind_speed_unit": "ms",
    }


# Answers

def test_answer_empty(tmp_path):
    with pytest.raises(ValueError, match="answer is empty"):
        check(tmp_path, b"")


def test_answer_malformed_json(tmp_path):
    with pytest.raises(ValueError, match="not valid JSON"):
        check(tmp_path, b'{"hourly": {"time": [')


def test_answer_api_error(tmp_path):
    reason = "Cannot initialize WeatherVariable from invalid String value showers"
    with pytest.raises(ValueError, match="refused the request: Cannot initialize"):
        check(tmp_path, {"error": True, "reason": reason})


def test_answer_not_in_utc(tmp_path):
    with pytest.raises(ValueError, match="not in UTC"):
        check(tmp_path, answer(utc_offset_seconds=-14400))


def test_answer_far_from_the_place(tmp_path):
    with pytest.raises(ValueError, match="grid point"):
        check(tmp_path, answer(latitude=-71.0, longitude=42.25))


def test_answer_without_hourly_values(tmp_path):
    data = answer()
    del data["hourly"]
    with pytest.raises(ValueError, match="no hourly time list"):
        check(tmp_path, data)


def test_answer_with_other_hours(tmp_path):
    with pytest.raises(ValueError, match="expected 2025-01-01T00:00 to 2025-01-02T23:00, 48 hours"):
        check(tmp_path, answer(end=date(2025, 1, 1)))


def test_answer_missing_a_value(tmp_path):
    data = answer()
    del data["hourly"]["snowfall"]
    with pytest.raises(ValueError, match=r"missing values \['snowfall'\]"):
        check(tmp_path, data)


def test_answer_value_with_wrong_length(tmp_path):
    data = answer()
    data["hourly"]["rain"] = data["hourly"]["rain"][:-1]
    with pytest.raises(ValueError, match="rain has 47 entries for 48 hours"):
        check(tmp_path, data)


def test_answer_value_that_is_not_a_number(tmp_path):
    data = answer()
    data["hourly"]["rain"][3] = "0.5"
    with pytest.raises(ValueError, match="not numbers"):
        check(tmp_path, data)


def test_answer_gaps_and_values_without_data_are_recorded(tmp_path):
    data = answer()
    data["hourly"]["snowfall"] = [None] * 48
    data["hourly"]["rain"][:24] = [None] * 24
    _, summary = check(tmp_path, data)
    assert summary["values"]["snowfall"] == {"unit": "mm", "hours_with_data": 0, "first": None, "last": None}
    assert summary["values"]["rain"] == {"unit": "mm", "hours_with_data": 24, "first": "2025-01-02T00:00", "last": "2025-01-02T23:00"}
    assert summary["grid"] == {"latitude": 42.25, "longitude": -71.0, "elevation": 3.0}
    assert (summary["first_hour"], summary["last_hour"], summary["hours"]) == ("2025-01-01T00:00", "2025-01-02T23:00", 48)


# The end of a set

def test_latest_full_day_skips_a_part_day(tmp_path):
    history = weather_sets(tmp_path)[0]
    client = FakeOpenMeteo(data_until={"archive-api.open-meteo.com": "2026-10-16T12:00"})
    assert latest_full_day(history, client, date(2026, 10, 21)) == date(2026, 10, 15)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(client.urls[0]).query))
    assert (query["start_date"], query["end_date"]) == ("2026-10-08", "2026-10-21")


def test_latest_full_day_none_without_data(tmp_path):
    history = weather_sets(tmp_path)[0]
    client = FakeOpenMeteo(data_until={"archive-api.open-meteo.com": "2026-01-01T00:00"})
    assert latest_full_day(history, client, date(2026, 10, 21)) is None


def test_set_without_recent_data_fails_and_stores_nothing(tmp_path):
    client = FakeOpenMeteo(data_until={"archive-api.open-meteo.com": "2026-01-01T00:00"})
    result = run(tmp_path, client, only=["history_era5"])
    assert result["failed"] == ["history_era5"]
    assert "no full day with values" in result["sets"][0]["reason"]
    assert year_files(tmp_path / "raw") == {}


# Runs

def test_first_run_stores_every_year_byte_for_byte(tmp_path):
    client = FakeOpenMeteo()
    result = run(tmp_path, client)
    assert result["failed"] == []
    assert len(client.urls) == 7  # history: the end check and 3 years; first hours: 3 years
    stored = year_files(tmp_path / "raw")
    assert sorted(stored) == [
        "first_hours_ecmwf_ifs/2024.json", "first_hours_ecmwf_ifs/2025.json", "first_hours_ecmwf_ifs/2026.json",
        "history_era5/2024.json", "history_era5/2025.json", "history_era5/2026.json",
    ]
    url = next(u for u in client.urls if "historical-forecast" in u and "start_date=2026-01-01" in u)
    assert stored["first_hours_ecmwf_ifs/2026.json"] == client.bodies[url]

    first_hours = manifest(tmp_path, "first_hours_ecmwf_ifs")
    assert first_hours["last_hour"] == "2026-10-21T23:00"  # yesterday: no hour after the download
    assert first_hours["downloaded_at"] == "2026-10-22T08:00:00+00:00"
    entry = first_hours["files"]["2026.json"]
    assert entry["grid"] == {"latitude": 42.25, "longitude": -71.0, "elevation": 3.0}
    assert entry["values"]["showers"]["hours_with_data"] == 0
    history = manifest(tmp_path, "history_era5")
    assert history["last_hour"] == "2026-10-16T23:00"  # the last full day ERA5 has
    assert history["files"]["2024.json"]["first_hour"] == "2024-11-01T00:00"

    runs = (tmp_path / "state" / "first_hours_ecmwf_ifs" / "_runs.jsonl").read_text().splitlines()
    assert [f["action"] for f in json.loads(runs[0])["files"]] == ["downloaded"] * 3
    assert all(re.fullmatch(r"\d{4}\.json", p.name) for p in (tmp_path / "raw").rglob("*") if p.is_file())


def test_rerun_on_the_same_day_downloads_nothing(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    before = year_files(tmp_path / "raw")
    client.urls.clear()
    result = run(tmp_path, client)
    assert result["failed"] == []
    assert len(client.urls) == 1 and "archive-api" in client.urls[0]  # only the ERA5 end check
    assert {f["action"] for s in result["sets"] for f in s["files"]} == {"skipped"}
    assert year_files(tmp_path / "raw") == before


def test_rerun_two_days_later_replaces_the_latest_year_whole(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    before = year_files(tmp_path / "raw")
    client.urls.clear()
    result = run(tmp_path, client, now=NOW + timedelta(days=2))
    assert result["failed"] == []
    assert sum("historical-forecast" in u for u in client.urls) == 1  # 2026 only
    after = year_files(tmp_path / "raw")
    times = json.loads(after["first_hours_ecmwf_ifs/2026.json"])["hourly"]["time"]
    assert times[0] == "2026-01-01T00:00" and times[-1] == "2026-10-23T23:00"
    assert len(times) == len(set(times)) == 296 * 24  # no hour twice
    latest = "first_hours_ecmwf_ifs/2026.json"
    assert {k: v for k, v in after.items() if k != latest} == {k: v for k, v in before.items() if k != latest}
    assert manifest(tmp_path, "first_hours_ecmwf_ifs")["last_hour"] == "2026-10-23T23:00"
    actions = {f["file"]: f["action"] for f in result["sets"][1]["files"]}
    assert actions == {"2024.json": "skipped", "2025.json": "skipped", "2026.json": "updated"}


def test_bad_answer_keeps_the_stored_file(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    path = tmp_path / "raw" / "first_hours_ecmwf_ifs" / "2026.json"
    old = path.read_bytes()
    client.answer_with = b'{"hourly": {"time": ['
    result = run(tmp_path, client, now=NOW + timedelta(days=1), only=["first_hours_ecmwf_ifs"])
    assert result["failed"] == ["first_hours_ecmwf_ifs/2026.json"]
    assert "not valid JSON" in result["sets"][0]["files"][2]["reason"]
    assert path.read_bytes() == old
    assert not list(path.parent.glob(".*.part"))
    assert manifest(tmp_path, "first_hours_ecmwf_ifs")["last_hour"] == "2026-10-21T23:00"


def test_network_error_keeps_the_stored_files(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    before = year_files(tmp_path / "raw")
    client.answer_with = OSError("connection reset")
    result = run(tmp_path, client, now=NOW + timedelta(days=1))
    # The ERA5 end check fails, so the whole history set waits; one first-hours year fails.
    assert result["failed"] == ["history_era5", "first_hours_ecmwf_ifs/2026.json"]
    assert year_files(tmp_path / "raw") == before


def test_dry_run_writes_nothing(tmp_path):
    client = FakeOpenMeteo()
    result = run(tmp_path, client, dry_run=True)
    assert not (tmp_path / "raw").exists() and not (tmp_path / "state").exists()
    assert {f["action"] for s in result["sets"] for f in s["files"]} == {"would_download"}
    assert len(client.urls) == 1  # only the ERA5 end check


def test_unknown_set_in_only(tmp_path):
    with pytest.raises(ValueError, match="unknown set"):
        run(tmp_path, FakeOpenMeteo(), only=["day_before_gfs"])


def test_changed_settings_need_a_new_name_or_force(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client, only=["history_era5"])
    moved = read_config(write_config(tmp_path, HISTORY_ROW.replace("42.36057", "42.5")), today=NOW.date())
    result = run_download(moved, tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    assert result["failed"] == ["history_era5"]
    assert "other settings" in result["sets"][0]["reason"]
    result = run_download(moved, tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW, force=True)
    assert result["failed"] == []
    assert manifest(tmp_path, "history_era5")["settings"]["latitude"] == 42.5


def test_broken_manifest_fails_the_set_and_says_how_to_fix_it(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    (tmp_path / "state" / "history_era5" / "_manifest.json").write_text("{not json")
    result = run(tmp_path, client)
    assert result["failed"] == ["history_era5"]
    assert "delete it to download the set again" in result["sets"][0]["reason"]


# Attribution

def test_attribution_rows_credit_every_file(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    rows = attribution_rows(weather_sets(tmp_path), tmp_path / "state")
    assert [r["file"] for r in rows] == [
        "history_era5/2024.json", "history_era5/2025.json", "history_era5/2026.json",
        "first_hours_ecmwf_ifs/2024.json", "first_hours_ecmwf_ifs/2025.json", "first_hours_ecmwf_ifs/2026.json",
    ]
    row = rows[-1]
    assert (row["attribution"], row["attribution_url"]) == ("Weather data by Open-Meteo.com", "https://open-meteo.com/")
    assert (row["license"], row["license_url"]) == ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/")
    assert (row["credit"], row["credit_url"]) == ("ECMWF", "https://example.org/e")
    assert (row["values_with_data"], row["values_without_data"]) == ("precipitation (mm); rain (mm)", "showers")
    assert (row["first_hour"], row["last_hour"], row["hours"]) == ("2026-01-01T00:00", "2026-10-21T23:00", 294 * 24)
    assert row["downloaded_at"] == "2026-10-22T08:00:00+00:00" and len(row["sha256"]) == 64
    assert (row["grid_latitude"], row["grid_longitude"], row["grid_elevation"]) == (42.25, -71.0, 3.0)
    assert rows[2]["credit"] == "Copernicus 2026"  # the download year goes into the Copernicus line


def test_main_exits_1_and_writes_no_attribution_after_a_failure(tmp_path, monkeypatch):
    client = FakeOpenMeteo()
    client.answer_with = b""
    use(monkeypatch, client)
    with pytest.raises(SystemExit) as stop:
        open_meteo.main(main_args(tmp_path))
    assert stop.value.code == 1
    assert not (tmp_path / "open-meteo.csv").exists()


def test_main_writes_the_attribution_file_after_a_clean_run(tmp_path, monkeypatch, capsys):
    use(monkeypatch, FakeOpenMeteo())
    open_meteo.main(main_args(tmp_path))
    rows = rows_of(tmp_path / "open-meteo.csv")
    assert len(rows) == 6 and list(rows[0]) == ATTRIBUTION_COLUMNS
    assert "wrote 6 rows" in capsys.readouterr().out


def test_only_keeps_the_rows_of_the_other_sets(tmp_path, monkeypatch):
    client = FakeOpenMeteo()
    use(monkeypatch, client)
    open_meteo.main(main_args(tmp_path))
    before = rows_of(tmp_path / "open-meteo.csv")
    # A teammate who has only set b: no history folders here at all.
    shutil.rmtree(tmp_path / "state" / "history_era5")
    shutil.rmtree(tmp_path / "raw" / "history_era5")
    use(monkeypatch, client, now=NOW + timedelta(days=1))
    open_meteo.main(main_args(tmp_path, "--only", "first_hours_ecmwf_ifs"))
    after = rows_of(tmp_path / "open-meteo.csv")
    assert [r["file"] for r in after] == [r["file"] for r in before]
    assert [r for r in after if r["set"] == "history_era5"] == [r for r in before if r["set"] == "history_era5"]
    assert next(r for r in after if r["file"] == "first_hours_ecmwf_ifs/2026.json")["last_hour"] == "2026-10-22T23:00"


def test_a_set_with_no_stored_files_here_keeps_its_rows(tmp_path):
    run(tmp_path, FakeOpenMeteo(), only=["first_hours_ecmwf_ifs"])
    kept = {column: "x" for column in ATTRIBUTION_COLUMNS} | {"set": "history_era5", "file": "history_era5/2021.json"}
    rows = attribution_rows(weather_sets(tmp_path), tmp_path / "state", existing=[kept])
    assert rows[0] == kept
    assert [r["set"] for r in rows[1:]] == ["first_hours_ecmwf_ifs"] * 3


def test_a_failed_run_leaves_the_attribution_file_alone(tmp_path, monkeypatch, capsys):
    client = FakeOpenMeteo()
    use(monkeypatch, client)
    open_meteo.main(main_args(tmp_path))
    attribution = tmp_path / "open-meteo.csv"
    written = (attribution.read_bytes(), attribution.stat().st_mtime_ns)
    client.answer_with = b""
    use(monkeypatch, client, now=NOW + timedelta(days=1))
    with pytest.raises(SystemExit):
        open_meteo.main(main_args(tmp_path))
    assert (attribution.read_bytes(), attribution.stat().st_mtime_ns) == written
    assert "not written: the run had failures" in capsys.readouterr().out


def test_an_attribution_file_with_other_columns_is_not_overwritten(tmp_path, monkeypatch):
    use(monkeypatch, FakeOpenMeteo())
    attribution = tmp_path / "open-meteo.csv"
    attribution.write_text("set,file\nx,y\n")
    with pytest.raises(SystemExit) as stop:
        open_meteo.main(main_args(tmp_path))
    assert stop.value.code == 1
    assert attribution.read_text() == "set,file\nx,y\n"


# Day-before forecasts (set c)

def day_before(tmp_path):
    return read_config(write_config(tmp_path, DAY_BEFORE_ROW), today=NOW.date())


def test_day_before_set_stores_both_offsets_through_yesterday(tmp_path):
    client = FakeOpenMeteo(value_starts=SNOW_STARTS)
    result = run_download(day_before(tmp_path), tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    assert result["failed"] == []
    assert len(client.urls) == 3  # 2024, 2025 and 2026; `yesterday` needs no end check
    stored = year_files(tmp_path / "raw")
    url = next(u for u in client.urls if "start_date=2026-01-01" in u)
    assert stored["day_before_ncep_gfs_global/2026.json"] == client.bodies[url]

    m = manifest(tmp_path, "day_before_ncep_gfs_global")
    assert (m["first_hour"], m["last_hour"]) == ("2024-11-15T00:00", "2026-10-21T23:00")
    values = m["files"]["2024.json"]["values"]
    assert values["temperature_2m_previous_day2"]["hours_with_data"] == 47 * 24
    assert values["snowfall_previous_day1"] == {
        "unit": "mm", "hours_with_data": 606, "first": "2024-12-06T18:00", "last": "2024-12-31T23:00",
    }
    assert values["snowfall_previous_day2"]["first"] == "2024-12-07T18:00"


def test_day_before_rerun_on_the_same_day_sends_no_request(tmp_path):
    client = FakeOpenMeteo(value_starts=SNOW_STARTS)
    sets = day_before(tmp_path)
    run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    before = year_files(tmp_path / "raw")
    client.urls.clear()
    result = run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    assert client.urls == [] and result["failed"] == []
    assert year_files(tmp_path / "raw") == before


@pytest.mark.parametrize(
    "broken, message",
    [
        (b"", "answer is empty"),
        (b'{"hourly": {"time": [', "not valid JSON"),
        ("no 2-day temperature", r"missing values \['temperature_2m_previous_day2'\]"),
    ],
)
def test_day_before_answer_empty_malformed_or_missing_a_value(tmp_path, broken, message):
    ws = day_before(tmp_path)[0]
    body = FakeOpenMeteo().get(request_url(ws, date(2025, 1, 1), date(2025, 1, 2)))
    if isinstance(broken, bytes):
        body = broken
    else:
        data = json.loads(body)
        del data["hourly"]["temperature_2m_previous_day2"]
        body = json.dumps(data).encode()
    with pytest.raises(ValueError, match=message):
        check_answer(body, ws, date(2025, 1, 1), date(2025, 1, 2))


def test_day_before_attribution_marks_validation_and_where_values_start(tmp_path):
    sets = day_before(tmp_path)
    run_download(sets, tmp_path / "raw", FakeOpenMeteo(value_starts=SNOW_STARTS), state_root=tmp_path / "state", now=NOW)
    rows = attribution_rows(sets, tmp_path / "state")
    assert [r["validation_test"] for r in rows] == ["yes"] * 3
    assert (rows[0]["credit"], rows[0]["credit_url"]) == ("NOAA NCEP", "https://example.org/n")
    assert "snowfall_previous_day1 (mm, 606 of 1128 hours, 2024-12-06T18:00 to 2024-12-31T23:00)" in rows[0]["values_with_data"]
    assert rows[1]["values_with_data"].startswith("temperature_2m_previous_day1 (mm); temperature_2m_previous_day2 (mm); ")


# The last day of a `yesterday` year (review: a year is not complete just because every hour came back)

FORECAST_HOST = "historical-forecast-api.open-meteo.com"
NEW_YEAR = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)  # yesterday is 2025-12-31


def first_hours(tmp_path):
    return read_config(write_config(tmp_path, FORECAST_ROW), today=NEW_YEAR.date())


def test_a_partial_december_31_is_fetched_again_after_new_year(tmp_path):
    sets = first_hours(tmp_path)
    client = FakeOpenMeteo(data_until={FORECAST_HOST: "2025-12-31T20:00"})  # 21:00 to 23:00 not out yet
    first = run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "state", now=NEW_YEAR)
    assert first["failed"] == []
    entry = manifest(tmp_path, "first_hours_ecmwf_ifs")["files"]["2025.json"]
    assert (entry["final_day_complete"], entry["final_day_gaps"]) == (False, ["precipitation", "rain"])
    assert "checked again until they are filled" in first["sets"][0]["files"][1]["reason"]

    client.data_until = {}  # the rest of 31 December is out now
    second = run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "state", now=NEW_YEAR + timedelta(days=1))
    actions = {f["file"]: f["action"] for f in second["sets"][0]["files"]}
    assert actions == {"2024.json": "skipped", "2025.json": "updated", "2026.json": "downloaded"}
    entry = manifest(tmp_path, "first_hours_ecmwf_ifs")["files"]["2025.json"]
    assert (entry["final_day_complete"], entry["final_day_gaps"]) == (True, [])
    stored = json.loads((tmp_path / "raw" / "first_hours_ecmwf_ifs" / "2025.json").read_text())["hourly"]
    assert stored["rain"][-1] == 0.5
    assert stored["showers"][-1] is None  # a value with no data at all stays missing and does not hold the day back

    client.urls.clear()
    run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "state", now=NEW_YEAR + timedelta(days=2))
    assert not any("start_date=2025" in u for u in client.urls)  # complete now: not fetched again


def run_2025(tmp_path, client, days):
    """Run set b `days` after NEW_YEAR. Returns the 2025 result, whether 2025 was asked for, and its entry."""
    client.urls.clear()
    result = run_download(first_hours(tmp_path), tmp_path / "raw", client, state_root=tmp_path / "state",
                          now=NEW_YEAR + timedelta(days=days))
    assert result["failed"] == []
    row = next(f for f in result["sets"][0]["files"] if f["file"] == "2025.json")
    asked = any("start_date=2025" in u for u in client.urls)
    return row, asked, manifest(tmp_path, "first_hours_ecmwf_ifs")["files"]["2025.json"]


def test_a_last_day_that_stays_incomplete_is_checked_every_week_after_the_retry_days(tmp_path):
    client = FakeOpenMeteo(data_until={FORECAST_HOST: "2025-12-31T20:00"})  # 21:00 to 23:00 never come out
    run_2025(tmp_path, client, 0)
    path = tmp_path / "raw" / "first_hours_ecmwf_ifs" / "2025.json"
    written = (path.read_bytes(), path.stat().st_mtime_ns)
    row, asked, entry = run_2025(tmp_path, client, 6)  # 2026-01-07, the last day of daily retries
    assert asked and row["action"] == "unchanged" and entry["final_day_checked"] == "2026-01-07"
    row, asked, entry = run_2025(tmp_path, client, 7)  # from now on, a week after the last check
    assert not asked and row["action"] == "skipped"
    assert "still has gaps in ['precipitation', 'rain'], next check on or after 2026-01-14" in row["reason"]
    row, asked, entry = run_2025(tmp_path, client, 13)
    assert asked and row["action"] == "unchanged" and entry["final_day_checked"] == "2026-01-14"
    assert (entry["final_day_complete"], entry["final_day_gaps"]) == (False, ["precipitation", "rain"])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == written  # the same answer never rewrites the file


def test_a_december_31_published_more_than_seven_days_late_is_picked_up(tmp_path):
    client = FakeOpenMeteo(data_until={FORECAST_HOST: "2025-12-31T20:00"})
    run_2025(tmp_path, client, 0)
    row, asked, entry = run_2025(tmp_path, client, 10)  # past the daily retries: checked, still incomplete
    assert asked and row["action"] == "unchanged" and entry["final_day_complete"] is False
    client.data_until = {}  # Open-Meteo publishes 21:00 to 23:00 eleven days after year-end
    row, asked, entry = run_2025(tmp_path, client, 11)  # the next check is a week after the last one
    assert not asked and entry["final_day_complete"] is False
    row, asked, entry = run_2025(tmp_path, client, 17)  # 2026-01-18
    assert asked and row["action"] == "updated"
    assert (entry["final_day_complete"], entry["final_day_gaps"]) == (True, [])
    assert "final_day_checked" not in entry
    stored = json.loads((tmp_path / "raw" / "first_hours_ecmwf_ifs" / "2025.json").read_text())["hourly"]
    assert stored["rain"][-1] == 0.5
    assert stored["showers"][-1] is None  # a value with no data at all is still not a gap
    row, asked, _ = run_2025(tmp_path, client, 30)
    assert not asked and row["action"] == "skipped"  # complete now: not fetched again


def test_an_older_entry_without_the_flag_is_judged_by_its_last_hours():
    entry = {"last_hour": "2025-12-31T23:00", "downloaded_at": "2026-01-01T03:00:00+00:00", "values": {
        "rain": {"hours_with_data": 8757, "last": "2025-12-31T20:00"},
        "showers": {"hours_with_data": 0, "last": None},
    }}
    assert not open_meteo.final_day_complete(entry)
    assert open_meteo.next_final_day_check(entry) == date(2026, 1, 8)  # no check date yet: a week after the download
    entry["values"]["rain"]["last"] = "2025-12-31T23:00"
    assert open_meteo.final_day_complete(entry)


# Folders and reruns (DVC: raw holds only the downloads; nothing unchanged is rewritten)

def test_raw_holds_only_year_files_and_state_holds_the_manifests_and_run_logs(tmp_path, monkeypatch):
    use(monkeypatch, FakeOpenMeteo())
    open_meteo.main(main_args(tmp_path))
    assert all(re.fullmatch(r"\d{4}\.json", name.split("/")[-1]) for name in files_in(tmp_path / "raw"))
    assert sorted(files_in(tmp_path / "state")) == [
        "first_hours_ecmwf_ifs/_manifest.json", "first_hours_ecmwf_ifs/_runs.jsonl",
        "history_era5/_manifest.json", "history_era5/_runs.jsonl",
    ]


def test_a_rerun_with_nothing_new_rewrites_no_raw_file_and_no_attribution(tmp_path, monkeypatch, capsys):
    use(monkeypatch, FakeOpenMeteo())
    open_meteo.main(main_args(tmp_path))
    raw, attribution = files_in(tmp_path / "raw"), tmp_path / "open-meteo.csv"
    written = (attribution.read_bytes(), attribution.stat().st_mtime_ns)
    capsys.readouterr()
    open_meteo.main(main_args(tmp_path))
    assert files_in(tmp_path / "raw") == raw
    assert (attribution.read_bytes(), attribution.stat().st_mtime_ns) == written
    assert "unchanged (6 rows)" in capsys.readouterr().out


def test_an_answer_with_the_same_bytes_leaves_the_file_and_its_entry_alone(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client, only=["first_hours_ecmwf_ifs"])
    before, entry = files_in(tmp_path / "raw"), manifest(tmp_path, "first_hours_ecmwf_ifs")["files"]["2026.json"]
    result = run(tmp_path, client, only=["first_hours_ecmwf_ifs"], force=True)
    assert {f["action"] for f in result["sets"][0]["files"]} == {"unchanged"}
    assert files_in(tmp_path / "raw") == before
    assert manifest(tmp_path, "first_hours_ecmwf_ifs")["files"]["2026.json"] == entry


def test_a_manifest_left_in_the_raw_folder_is_read_in_place(tmp_path, caplog):
    client = FakeOpenMeteo()
    sets = weather_sets(tmp_path)
    run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "raw", now=NOW)  # the layout before the review
    old = (tmp_path / "raw" / "first_hours_ecmwf_ifs" / "_manifest.json").read_bytes()
    client.urls.clear()
    with caplog.at_level(logging.WARNING, logger="open_meteo"):
        result = run_download(sets, tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    assert result["failed"] == [] and len(client.urls) == 1  # only the ERA5 end check: nothing downloaded again
    assert (tmp_path / "raw" / "first_hours_ecmwf_ifs" / "_manifest.json").read_bytes() == old
    assert "move it there" in caplog.text


# Network (review: stop on HTTP 429, retry 5xx, keep the status)

def http_error(code, body=b"", unreadable=False):
    class Unreadable(io.BytesIO):
        def read(self, *args):
            raise ConnectionResetError("reset while reading the body")

    return urllib.error.HTTPError("https://archive-api.open-meteo.com/v1/archive", code, "error", {},
                                  Unreadable() if unreadable else io.BytesIO(body))


def answers(monkeypatch, *outcomes):
    """urlopen that gives these outcomes in turn: bytes for an answer, or an exception to raise."""
    calls, queue = [], list(outcomes)

    def urlopen(request, timeout=None):
        calls.append(request.full_url)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome)

    monkeypatch.setattr(open_meteo.urllib.request, "urlopen", urlopen)
    return calls


def client_with_waits(sleeps):
    return open_meteo.OpenMeteoClient(pause=0, waits=(5, 15), sleep=sleeps.append)


URL = "https://archive-api.open-meteo.com/v1/archive?x=1"


def test_a_server_error_is_tried_again_after_a_wait(monkeypatch):
    calls, sleeps = answers(monkeypatch, http_error(502), http_error(503), b"{}"), []
    assert client_with_waits(sleeps).get(URL) == b"{}"
    assert len(calls) == 3 and [s for s in sleeps if s] == [5, 15]


def test_a_lasting_server_error_fails_with_its_status(monkeypatch):
    calls, sleeps = answers(monkeypatch, http_error(502), http_error(502), http_error(502)), []
    with pytest.raises(open_meteo.ApiError, match="HTTP 502") as error:
        client_with_waits(sleeps).get(URL)
    assert error.value.status == 502 and len(calls) == 3


def test_http_429_fails_at_once_without_waiting(monkeypatch):
    calls = answers(monkeypatch, http_error(429, b'{"error": true, "reason": "Minutely API request limit exceeded"}'))
    sleeps = []
    with pytest.raises(open_meteo.RateLimited, match="HTTP 429: Minutely API request limit exceeded"):
        client_with_waits(sleeps).get(URL)
    assert len(calls) == 1 and not [s for s in sleeps if s]


def test_the_status_is_kept_when_the_error_body_cannot_be_read(monkeypatch):
    answers(monkeypatch, http_error(502, unreadable=True))
    client = open_meteo.OpenMeteoClient(pause=0, waits=(), sleep=lambda seconds: None)
    with pytest.raises(open_meteo.ApiError, match=r"HTTP 502: the error body could not be read \(ConnectionResetError\)") as error:
        client.get(URL)
    assert error.value.status == 502


def test_a_client_error_is_not_tried_again(monkeypatch):
    calls = answers(monkeypatch, http_error(400, b'{"error": true, "reason": "Cannot initialize WeatherVariable"}'))
    with pytest.raises(open_meteo.ApiError, match="HTTP 400: Cannot initialize WeatherVariable"):
        client_with_waits([]).get(URL)
    assert len(calls) == 1


class LimitAfter(FakeOpenMeteo):
    """Answers `ok` requests, then HTTP 429 for every later one."""

    def __init__(self, ok, **kwargs):
        super().__init__(**kwargs)
        self.ok = ok

    def get(self, url):
        if len(self.urls) >= self.ok:
            self.urls.append(url)
            raise open_meteo.RateLimited(429, "HTTP 429: Minutely API request limit exceeded")
        return super().get(url)


def test_http_429_stops_the_run(tmp_path):
    client = LimitAfter(ok=2)  # the ERA5 end check and history 2024, then the limit
    result = run(tmp_path, client)
    assert len(client.urls) == 3  # no request after the 429
    history, first = result["sets"]
    assert [f["action"] for f in history["files"]] == ["downloaded", "failed", "not_tried"]
    assert first["reason"].startswith("not tried: the run stopped after HTTP 429")
    assert result["stopped"].startswith("HTTP 429") and "first_hours_ecmwf_ifs" in result["failed"]
    assert set(manifest(tmp_path, "history_era5")["files"]) == {"2024.json"}


# Manifest and config checks (review: malformed entries, a moved start date)

def edit_manifest(tmp_path, name, change):
    path = tmp_path / "state" / name / "_manifest.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda m: m["files"]["2025.json"].pop("sha256"), "entry '2025.json' lacks ['sha256']"),
        (lambda m: m["files"]["2025.json"].update(hours="many"), "entry '2025.json' has hours 'many'"),
        (lambda m: m["files"]["2025.json"].update(sha256="abc"), "entry '2025.json' has no valid sha256"),
        (lambda m: m["files"]["2025.json"].update(last_hour="yesterday"), "is not a time"),
        (lambda m: m["files"]["2025.json"].update(values=[]), "has values without hours_with_data"),
        (lambda m: m["files"]["2025.json"].update(final_day_checked="soon"), "has final_day_checked 'soon', which is not a date"),
        (lambda m: m["files"].update({"2025.json": "lost"}), "entry '2025.json' is not an object"),
        (lambda m: m.update(files=[]), "'files' is not an object"),
    ],
)
def test_a_malformed_manifest_fails_the_set_with_a_clear_message(tmp_path, change, message):
    client = FakeOpenMeteo()
    run(tmp_path, client)
    edit_manifest(tmp_path, "first_hours_ecmwf_ifs", change)
    result = run(tmp_path, client)
    assert result["failed"] == ["first_hours_ecmwf_ifs"]
    assert message in result["sets"][1]["reason"]


def later_start(tmp_path):
    return read_config(write_config(tmp_path, FORECAST_ROW.replace("2024-11-01", "2025-03-01")), today=NOW.date())


def test_a_later_start_needs_force_and_force_drops_the_earlier_years(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client, only=["first_hours_ecmwf_ifs"])
    result = run_download(later_start(tmp_path), tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    assert result["failed"] == ["first_hours_ecmwf_ifs"]
    assert "start moved from 2024-11-01 to 2025-03-01" in result["sets"][0]["reason"]
    result = run_download(later_start(tmp_path), tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW, force=True)
    assert result["failed"] == [] and result["sets"][0]["dropped"] == ["2024.json"]
    m = manifest(tmp_path, "first_hours_ecmwf_ifs")
    assert sorted(m["files"]) == ["2025.json", "2026.json"] and m["start"] == "2025-03-01"
    assert m["files"]["2025.json"]["first_hour"] == m["first_hour"] == "2025-03-01T00:00"
    assert sorted(files_in(tmp_path / "raw")) == ["first_hours_ecmwf_ifs/2025.json", "first_hours_ecmwf_ifs/2026.json"]


def test_a_manifest_without_a_start_uses_its_earliest_hour(tmp_path):
    client = FakeOpenMeteo()
    run(tmp_path, client, only=["first_hours_ecmwf_ifs"])
    edit_manifest(tmp_path, "first_hours_ecmwf_ifs", lambda m: m.pop("start"))
    result = run_download(later_start(tmp_path), tmp_path / "raw", client, state_root=tmp_path / "state", now=NOW)
    assert "start moved from 2024-11-01 to 2025-03-01" in result["sets"][0]["reason"]
    assert run(tmp_path, client, only=["first_hours_ecmwf_ifs"])["failed"] == []  # the unchanged config still runs
