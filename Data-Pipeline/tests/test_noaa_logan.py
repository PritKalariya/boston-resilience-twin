import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import credentials
import noaa_logan
import raw_fetch as rf

NOW = datetime(2016, 2, 15, tzinfo=timezone.utc)
TODAY = NOW.date()


def rows_for(start: date, end: date, n: int):
    """n results spread over the period, cycling through the datatypes."""
    days = (end - start).days + 1
    return [{"date": f"{start + timedelta(days=i % days)}T00:00:00", "datatype": noaa_logan.DATATYPES[i % 8],
             "station": noaa_logan.STATION, "attributes": ",,W,", "value": i} for i in range(n)]


class FakeCdo:
    """Pages like CDO: 1,000 results per page, offset from 1, {} when there is no data."""

    def __init__(self, per_year=None, overrides=None):
        self.calls, self.per_year, self.overrides = [], per_year or {}, overrides or {}

    def __call__(self, url):
        q = parse_qs(urlsplit(url).query)
        start, end, offset = date.fromisoformat(q["startdate"][0]), date.fromisoformat(q["enddate"][0]), int(q["offset"][0])
        self.calls.append((start.year, offset, sorted(q["datatypeid"])))
        if (start.year, offset) in self.overrides:
            return self.overrides[(start.year, offset)]
        total = self.per_year.get(start.year, 2900)
        if total == 0:
            return b"{}"
        rows = rows_for(start, end, total)[offset - 1: offset - 1 + noaa_logan.PAGE]
        return json.dumps({"metadata": {"resultset": {"offset": offset, "count": total, "limit": 1000}}, "results": rows}).encode()


def refresh(tmp_path, fake, today=TODAY, now=NOW):
    return noaa_logan.refresh(tmp_path, fake, today, now, pause=lambda s: None)


def test_pages_are_joined_into_one_file_per_year(tmp_path):
    fake = FakeCdo({2015: 2900, 2016: 300})
    records = refresh(tmp_path, fake)
    assert [c[:2] for c in fake.calls] == [(2015, 1), (2015, 1001), (2015, 2001), (2016, 1)]
    assert fake.calls[0][2] == sorted(noaa_logan.DATATYPES)
    stored = json.loads((tmp_path / "2015.json").read_text())
    assert len(stored["results"]) == 2900 and stored["metadata"]["resultset"]["count"] == 2900
    assert stored["results"][1500]["value"] == 1500  # values unchanged, pages in order
    assert records == [{"file": "2015.json", "action": "downloaded", "rows": 2900},
                       {"file": "2016.json", "action": "downloaded", "rows": 300}]


def test_rerun_skips_settled_years(tmp_path):
    refresh(tmp_path, FakeCdo({2015: 2900, 2016: 300}))
    fake = FakeCdo({2015: 2900, 2016: 320})
    records = refresh(tmp_path, fake, date(2016, 2, 20), datetime(2016, 2, 20, tzinfo=timezone.utc))
    # 2015 ended 2015-12-31 and was downloaded 2016-02-15, 46 days later: final.
    assert records[0] == {"file": "2015.json", "action": "final"}
    assert records[1]["action"] == "updated" and records[1]["rows"] == 320
    assert {c[0] for c in fake.calls} == {2016}


def test_a_year_downloaded_too_soon_is_fetched_again(tmp_path):
    refresh(tmp_path, FakeCdo({2015: 2900}), date(2016, 1, 5), datetime(2016, 1, 5, tzinfo=timezone.utc))
    fake = FakeCdo({2015: 2900, 2016: 40})
    records = refresh(tmp_path, fake)
    assert records[0]["action"] == "unchanged"  # same values, now settled
    assert any(c[0] == 2015 for c in fake.calls)
    assert refresh(tmp_path, FakeCdo({2016: 40}))[0]["action"] == "final"


def test_no_data_yet_in_a_new_year_is_skipped(tmp_path):
    records = refresh(tmp_path, FakeCdo({2015: 2900, 2016: 0}), date(2016, 1, 3), datetime(2016, 1, 3, tzinfo=timezone.utc))
    assert records[1] == {"file": "2016.json", "action": "no data yet"}
    assert not (tmp_path / "2016.json").exists()


def test_no_data_for_a_year_long_over_fails(tmp_path):
    with pytest.raises(rf.FetchError, match="no data for a year long over"):
        refresh(tmp_path, FakeCdo({2015: 0}))


@pytest.mark.parametrize("body, match", [
    (b"", "empty"),
    (b"not json", "not valid JSON"),
    (b"[]", "not a JSON object"),
    (json.dumps({"results": []}).encode(), "no metadata"),
    (json.dumps({"metadata": {"resultset": {"count": 5}}, "results": [{"date": "2015-01-01T00:00:00"}]}).encode(), "lacks a date, datatype or value"),
    (json.dumps({"metadata": {"resultset": {"count": 1}}, "results": [{"date": "2014-12-31T00:00:00", "datatype": "PRCP", "value": 0}]}).encode(), "outside"),
    (json.dumps({"metadata": {"resultset": {"count": 1}}, "results": [{"date": "2015-01-01T00:00:00", "datatype": "TAVG", "value": 0}]}).encode(), "unexpected datatype"),
])
def test_bad_answers_fail_and_store_nothing(tmp_path, body, match):
    with pytest.raises(rf.FetchError, match=match):
        refresh(tmp_path, FakeCdo(overrides={(2015, 1): body}))
    assert not (tmp_path / "2015.json").exists()


def test_a_lost_page_fails_the_count_check(tmp_path):
    fake = FakeCdo({2015: 2900}, overrides={(2015, 2001): json.dumps({}).encode()})
    with pytest.raises(rf.FetchError, match="got 2000 results, CDO said 2900"):
        refresh(tmp_path, fake)


def test_pages_are_spaced_for_the_rate_limit(tmp_path):
    pauses = []
    noaa_logan.refresh(tmp_path, FakeCdo({2015: 2900, 2016: 10}), TODAY, NOW, pause=pauses.append)
    assert pauses == [noaa_logan.PAUSE_SECONDS] * 2  # between the three 2015 pages


def test_missing_token_fails_naming_it(monkeypatch, tmp_path):
    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    with pytest.raises(credentials.MissingCredentialError, match="NOAA_CDO_TOKEN is not set"):
        noaa_logan.main(["--raw-dir", str(tmp_path)])
