import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import raw_fetch as rf
import tides

TODAY = date(2015, 9, 15)
NOW = datetime(2015, 9, 15, 12, tzinfo=timezone.utc)


def answer(product: str, begin: str, end: str) -> bytes:
    """A CO-OPS-shaped answer: one row at the start and one at the end of the range."""
    key = "data" if product == "hourly_height" else "predictions"
    rows = [{"t": f"{d[:4]}-{d[4:6]}-{d[6:]} 00:00", "v": "1.234"} for d in (begin, end)]
    body = {key: rows}
    if key == "data":
        body["metadata"] = {"id": "8443970", "name": "Boston"}
    return json.dumps(body).encode()


class FakeCoops:
    """Answers like CO-OPS; overrides return a fixed body for one product and begin date."""

    def __init__(self, overrides=None):
        self.calls, self.overrides = [], overrides or {}

    def __call__(self, url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.calls.append(q)
        key = (q["product"], q["begin_date"])
        if key in self.overrides:
            return self.overrides[key]
        return answer(q["product"], q["begin_date"], q["end_date"])


def test_first_run_stores_each_half_year_for_both_products(tmp_path):
    fake = FakeCoops()
    records = tides.refresh(tmp_path, fake, TODAY, NOW)
    assert sorted(p.name for p in (tmp_path / "observed").glob("*.json")) == ["2015-H1.json", "2015-H2.json", "_manifest.json"]
    assert sorted(p.name for p in (tmp_path / "predicted").glob("2015-*.json")) == ["2015-H1.json", "2015-H2.json"]
    assert {r["action"] for r in records} == {"downloaded"}
    first = fake.calls[0]
    assert first["station"] == "8443970" and first["datum"] == "MLLW" and first["time_zone"] == "gmt"
    assert first["units"] == "metric"
    assert (tmp_path / "observed" / "2015-H1.json").read_bytes() == answer("hourly_height", "20150101", "20150630")


def test_requests_stay_under_the_365_day_limit_in_a_leap_year(tmp_path):
    fake = FakeCoops()
    tides.refresh(tmp_path, fake, date(2016, 12, 31), datetime(2016, 12, 31, tzinfo=timezone.utc))
    for q in fake.calls:
        span = date.fromisoformat(f"{q['end_date'][:4]}-{q['end_date'][4:6]}-{q['end_date'][6:]}") - date.fromisoformat(
            f"{q['begin_date'][:4]}-{q['begin_date'][4:6]}-{q['begin_date'][6:]}")
        assert span.days < 365


def test_rerun_skips_final_halves_and_refreshes_the_current_one(tmp_path):
    tides.refresh(tmp_path, FakeCoops(), TODAY, NOW)
    later = date(2015, 9, 20)
    fake = FakeCoops()
    records = tides.refresh(tmp_path, fake, later, datetime(2015, 9, 20, tzinfo=timezone.utc))
    by = {(r["product"], r["file"]): r["action"] for r in records}
    # H1 ended 2015-06-30: predicted is final at once, observed waits 45 days (downloaded 2015-09-15: final).
    assert by[("predicted", "2015-H1.json")] == "final"
    assert by[("observed", "2015-H1.json")] == "final"
    assert by[("observed", "2015-H2.json")] == "updated"  # the half grew from 09-15 to 09-20
    assert {q["begin_date"] for q in fake.calls} == {"20150701"}


def test_observed_half_is_not_final_until_it_settles(tmp_path):
    tides.refresh(tmp_path, FakeCoops(), date(2015, 7, 10), datetime(2015, 7, 10, tzinfo=timezone.utc))
    fake = FakeCoops()
    records = tides.refresh(tmp_path, fake, date(2015, 7, 20), datetime(2015, 7, 20, tzinfo=timezone.utc))
    by = {(r["product"], r["file"]): r["action"] for r in records}
    assert by[("observed", "2015-H1.json")] == "unchanged"  # downloaded only 10 days after June 30
    assert by[("predicted", "2015-H1.json")] == "final"


def test_no_data_yet_for_a_recent_half_is_skipped_not_stored(tmp_path):
    no_data = json.dumps({"error": {"message": "No data was found. This product may not be offered..."}}).encode()
    fake = FakeCoops({("hourly_height", "20150701"): no_data})
    records = tides.refresh(tmp_path, fake, TODAY, NOW)
    assert {"product": "observed", "file": "2015-H2.json", "action": "no data yet"} in records
    assert not (tmp_path / "observed" / "2015-H2.json").exists()


def test_no_data_for_a_half_long_over_fails(tmp_path):
    no_data = json.dumps({"error": {"message": "No data was found."}}).encode()
    fake = FakeCoops({("hourly_height", "20150101"): no_data})
    with pytest.raises(rf.FetchError, match="no data for a period long over"):
        tides.refresh(tmp_path, fake, TODAY, NOW)


@pytest.mark.parametrize("body, match", [
    (b"", "empty"),
    (b"<html>maintenance</html>", "not valid JSON"),
    (json.dumps({"error": {"message": "Range Limit Exceeded"}}).encode(), "CO-OPS refused the request: Range Limit"),
    (json.dumps({"data": []}).encode(), "no 'data' list"),
    (json.dumps({"data": [{"t": "2015-01-01 00:00"}]}).encode(), "lack a time or value"),
    (json.dumps({"data": [{"t": "2014-12-31 23:00", "v": "1"}]}).encode(), "outside"),
])
def test_bad_answers_fail_and_store_nothing(tmp_path, body, match):
    fake = FakeCoops({("hourly_height", "20150101"): body})
    with pytest.raises(rf.FetchError, match=match):
        tides.refresh(tmp_path, fake, TODAY, NOW)
    assert not (tmp_path / "observed" / "2015-H1.json").exists()


def test_a_failed_rerun_keeps_the_stored_file(tmp_path):
    tides.refresh(tmp_path, FakeCoops(), date(2015, 7, 10), datetime(2015, 7, 10, tzinfo=timezone.utc))
    stored = (tmp_path / "observed" / "2015-H2.json").read_bytes()
    with pytest.raises(rf.FetchError):
        tides.refresh(tmp_path, FakeCoops({("hourly_height", "20150701"): b"garbage"}), TODAY, NOW)
    assert (tmp_path / "observed" / "2015-H2.json").read_bytes() == stored
