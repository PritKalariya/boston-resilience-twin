import io
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

import pytest

import raw_fetch as rf


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_get_returns_the_body_and_sends_the_headers(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen.update(request.headers, timeout=timeout)
        return Response(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert rf.get("https://example.org/x", {"User-Agent": "brt"}) == b'{"ok": true}'
    assert seen["User-agent"] == "brt" and seen["timeout"] == rf.TIMEOUT_SECONDS


def test_http_errors_carry_the_status_and_message_but_not_the_headers(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"message":"Token parameter is required."}'))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(rf.FetchError, match="HTTP 400 Bad Request: .*Token parameter is required") as e:
        rf.get("https://example.org/x", {"token": "secret-token-value"})
    assert "secret-token-value" not in str(e.value)


def test_network_errors_name_the_host(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(rf.FetchError, match="could not reach api.weather.gov: timed out"):
        rf.get("https://api.weather.gov/points/1,2", {})


def test_half_years_split_a_leap_year_and_trim_the_ends():
    periods = rf.half_years(date(2016, 3, 1), date(2016, 9, 15))
    assert [(p.name, p.start, p.end) for p in periods] == [
        ("2016-H1", date(2016, 3, 1), date(2016, 6, 30)),
        ("2016-H2", date(2016, 7, 1), date(2016, 9, 15)),
    ]


def test_is_final_needs_the_whole_period_and_the_settle_time():
    period = rf.Period("2015", date(2015, 1, 1), date(2015, 12, 31))
    entry = {"start": "2015-01-01", "end": "2015-12-31", "downloaded_at": "2016-01-31T00:00:00+00:00"}
    assert rf.is_final(entry, period, 31)  # 2015-12-31 + 31 days = 2016-01-31
    assert not rf.is_final(entry, period, 32)
    assert not rf.is_final({**entry, "end": "2015-10-01"}, period, 0)  # stored before the year ended
    assert not rf.is_final(None, period, 0)


def test_store_writes_atomically_and_records_the_file(tmp_path):
    period = rf.Period("2015", date(2015, 1, 1), date(2015, 12, 31))
    now = datetime(2016, 2, 1, tzinfo=timezone.utc)
    raw, state = tmp_path / "raw", tmp_path / "state"
    manifest = {}
    assert rf.store(raw, state, manifest, "2015.json", period, b"abc", now, rows=3) == "downloaded"
    assert rf.store(raw, state, manifest, "2015.json", period, b"abc", now) == "unchanged"
    assert rf.store(raw, state, manifest, "2015.json", period, b"abcd", now) == "updated"
    assert (raw / "2015.json").read_bytes() == b"abcd"
    assert rf.load_manifest(state)["2015.json"]["sha256"] == rf.sha256(b"abcd")
    assert sorted(p.name for p in raw.iterdir()) == ["2015.json"]  # no manifest or .part left in raw/


def test_a_broken_manifest_fails_with_how_to_fix_it(tmp_path):
    (tmp_path / rf.MANIFEST).write_text("{broken")
    with pytest.raises(rf.FetchError, match="delete it to download the folder again"):
        rf.load_manifest(tmp_path)
