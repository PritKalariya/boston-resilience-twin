import json

import pytest

import credentials
import nws
import raw_fetch as rf

GRID = "https://api.weather.gov/gridpoints/BOX/71,101"


def gridpoint(update="2026-10-26T01:49:45+00:00", **values):
    props = {"updateTime": update}
    for name in nws.GRID_VALUES:
        props[name] = {"uom": "wmoUnit:mm", "values": values.get(name, [{"validTime": "2026-10-26T00:00:00+00:00/PT1H", "value": 0.5}])}
    return json.dumps({"type": "Feature", "properties": props}).encode()


def fake_nws(grid=None, alerts=None, points=None):
    calls = []

    def fetch(url):
        calls.append(url)
        if "/points/" in url:
            return points if points is not None else json.dumps({"properties": {"forecastGridData": GRID}}).encode()
        if url == GRID:
            return grid if grid is not None else gridpoint()
        if "/alerts/active" in url:
            return alerts if alerts is not None else json.dumps(
                {"type": "FeatureCollection", "features": [], "updated": "2026-10-26T04:19:58+00:00"}).encode()
        raise AssertionError(url)

    fetch.calls = calls
    return fetch


def test_forecast_snapshot_is_named_by_its_update_time(tmp_path):
    fetch = fake_nws()
    action, path = nws.forecasts(tmp_path, fetch)
    assert action == "saved"
    assert path == tmp_path / "2026-10-26" / "20261026T014945Z.json"
    assert path.read_bytes() == gridpoint()
    assert fetch.calls == [f"{nws.API}/points/{nws.POINT}", GRID]


def test_rerun_with_the_same_forecast_writes_nothing(tmp_path):
    nws.forecasts(tmp_path, fake_nws())
    action, _ = nws.forecasts(tmp_path, fake_nws())
    assert action == "unchanged"
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_a_new_forecast_adds_a_snapshot_and_keeps_the_old_one(tmp_path):
    nws.forecasts(tmp_path, fake_nws())
    nws.forecasts(tmp_path, fake_nws(grid=gridpoint(update="2026-10-26T07:30:00+00:00")))
    assert sorted(p.name for p in tmp_path.rglob("*.json")) == ["20261026T014945Z.json", "20261026T073000Z.json"]


def test_alerts_snapshot_is_named_by_its_updated_time(tmp_path):
    action, path = nws.alerts(tmp_path, fake_nws())
    assert action == "saved" and path.name == "20261026T041958Z.json"
    assert json.loads(path.read_text())["type"] == "FeatureCollection"


def test_alerts_ask_for_the_zone_and_the_county(tmp_path):
    fetch = fake_nws()
    nws.alerts(tmp_path, fetch)
    assert fetch.calls == [f"{nws.API}/alerts/active?zone=MAZ015,MAC025"]


def test_local_time_stamps_file_under_their_utc_date(tmp_path):
    _, path = nws.forecasts(tmp_path, fake_nws(grid=gridpoint(update="2026-10-25T21:00:00-04:00")))
    assert path == tmp_path / "2026-10-26" / "20261026T010000Z.json"


@pytest.mark.parametrize("grid, match", [
    (b"", "empty"),
    (b"{oops", "not valid JSON"),
    (json.dumps({"type": "Feature"}).encode(), "no properties"),
    (json.dumps({"properties": {"updateTime": "2026-10-26T01:49:45+00:00"}}).encode(), "lacks"),
    (gridpoint(**{name: [] for name in nws.GRID_VALUES}), "every forecast value list is empty"),
    (gridpoint(update="yesterday"), "not a timestamp"),
    (gridpoint(update="2026-10-26T01:49:45"), "no time zone"),
])
def test_bad_forecasts_fail_and_store_nothing(tmp_path, grid, match):
    with pytest.raises(rf.FetchError, match=match):
        nws.forecasts(tmp_path, fake_nws(grid=grid))
    assert not list(tmp_path.rglob("*.json"))


def test_a_points_answer_without_a_grid_link_fails(tmp_path):
    with pytest.raises(rf.FetchError, match="no forecastGridData link"):
        nws.forecasts(tmp_path, fake_nws(points=json.dumps({"properties": {}}).encode()))


@pytest.mark.parametrize("body, match", [
    (b"", "empty"),
    (json.dumps({"type": "Feature"}).encode(), "not a FeatureCollection"),
    (json.dumps({"type": "FeatureCollection", "features": []}).encode(), "not a timestamp"),
])
def test_bad_alerts_fail_and_store_nothing(tmp_path, body, match):
    with pytest.raises(rf.FetchError, match=match):
        nws.alerts(tmp_path, fake_nws(alerts=body))
    assert not list(tmp_path.rglob("*.json"))


def test_missing_user_agent_fails_naming_it(monkeypatch, tmp_path):
    monkeypatch.delenv("NWS_USER_AGENT", raising=False)
    with pytest.raises(credentials.MissingCredentialError, match="NWS_USER_AGENT is not set"):
        nws.main(["forecasts", "--raw-dir", str(tmp_path)])
