import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import alert_history
from alert_history import (
    DOWNLOADS,
    LEAKY_COLUMNS,
    OUT_COLUMNS,
    PREPARED_COLUMNS,
    UNMAPPED,
    FreshnessError,
    alert_features,
    bulletin_path,
    bulletin_url,
    check_freshness,
    check_raw,
    cutoff_utc,
    day_start_utc,
    download_bulletins,
    download_years,
    first_begin,
    load_alert_map,
    prepare,
    raw_path,
    read_bulletin,
    read_raw,
    ugc_codes,
    with_first_begins,
    year_url,
    years_to_pull,
)

REPO = Path(__file__).resolve().parents[2]
MAP = REPO / "docs" / "data" / "alert-map.csv"
SETTINGS = REPO / "Data-Pipeline" / "config" / "labels_features.csv"
GROUPS = ["snow_ice", "tree", "flooding"]
HEADER = ["wfo", "utc_issue", "utc_expire", "utc_prodissue", "utc_init_expire", "phenomena", "gtype",
          "significance", "eventid", "status", "ugc", "utc_updated", "product_id", "vtec_year"]
NOW = datetime(2026, 10, 26, 22, 30, tzinfo=timezone.utc)


def row(ugc="MAZ015", phenomena="WS", significance="W", eventid=1, first="2015-01-25 20:58",
        first_end="201501280600", year=2015, product_id=None, status="CAN", expire="2015-01-27 23:37",
        updated="2015-01-27 23:37"):
    stamp = pd.Timestamp(first).strftime("%Y%m%d%H%M")
    return {
        "wfo": "BOX", "utc_issue": "2015-01-27 00:00", "utc_expire": expire,
        "utc_prodissue": first, "utc_init_expire": first_end, "phenomena": phenomena, "gtype": "C",
        "significance": significance, "eventid": str(eventid), "status": status, "ugc": ugc,
        "utc_updated": updated, "product_id": product_id or f"{stamp}-KBOX-WWUS41-WSWBOX",
        "vtec_year": str(year),
    }


def running(**fields):
    """An alert IEM shows as still running: last updated at its first bulletin, ending as first announced."""
    first, first_end = fields.get("first", "2015-01-25 20:58"), fields.get("first_end", "201501280600")
    end = pd.to_datetime(first_end, format="%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
    return row(status="CON", expire=end, updated=first, **fields)


def ended(status, at, **fields):
    """An alert cancelled (CAN) or upgraded (UPG) at `at` (UTC), as IEM records it: utc_expire = utc_updated."""
    return row(status=status, expire=at, updated=at, **fields)


def raw(*rows):
    return pd.DataFrame(list(rows), columns=HEADER).astype(str)


def boston_map():
    return load_alert_map(MAP)


def write_csv(path, *rows):
    raw(*rows).to_csv(path, index=False)


def record_pulls(state_dir, pulls):
    """_downloads.json as if each year had been pulled at the given time."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / DOWNLOADS).write_text(json.dumps({str(year): {"downloaded_at": at, "rows": 1} for year, at in pulls.items()}))


def prepared(rows, begins=None):
    """prepare's alerts with first_begin_utc: each begins at its first bulletin unless begins gives its
    first-announced begin (UTC) by event number."""
    alerts, _ = prepare(rows, boston_map())
    begins = begins or {}
    first = [pd.Timestamp(begins[event], tz="UTC") if event in begins else sent
             for event, sent in zip(alerts["eventid"], alerts["first_bulletin_utc"])]
    return alerts.assign(first_begin_utc=pd.to_datetime(pd.Series(first, index=alerts.index, dtype=object), utc=True))[OUT_COLUMNS]


def bulletin_text(*segments):
    """A first bulletin as IEM's text service serves it: one segment per (UGC block, VTEC strings)."""
    body = "\n".join(f"\n{ugcs}\n{vtecs}\nSuffolk MA-\n358 PM EST Sun Jan 25 2015\n\n...an alert...\n\n$$" for ugcs, vtecs in segments)
    return f"041 \nWWUS41 KBOX 252058\nWSWBOX\n\nURGENT - WINTER WEATHER MESSAGE\n{body}\n"


def write_bulletin(raw_dir, product_id, *segments):
    path = bulletin_path(raw_dir, product_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bulletin_text(*segments), encoding="utf-8")


# Prepare


def test_prepare_keeps_boston_rows_and_only_safe_fields():
    alerts, report = prepare(raw(row(), row(ugc="MAC025", phenomena="SV", eventid=7), row(ugc="MAZ014"), row(ugc="")),
                             boston_map())
    assert list(alerts.columns) == PREPARED_COLUMNS
    assert not set(LEAKY_COLUMNS) & set(alerts.columns)
    assert sorted(alerts["ugc"]) == ["MAC025", "MAZ015"]
    assert dict(zip(alerts["ugc"], alerts["area"])) == {"MAZ015": "zone", "MAC025": "county"}
    ws = alerts[alerts["phenomena"] == "WS"].iloc[0]
    assert ws["first_bulletin_utc"] == pd.Timestamp("2015-01-25 20:58", tz="UTC")
    assert ws["first_end_utc"] == pd.Timestamp("2015-01-28 06:00", tz="UTC")
    assert (ws["alert_type"], ws["storm_groups"]) == ("winter storm warning", "snow_ice")
    assert report.loc[2015, "zone"] == 1 and report.loc[2015, "county"] == 1


def test_product_id_must_start_with_the_first_bulletin():
    with pytest.raises(ValueError, match="product_id"):
        prepare(raw(row(product_id="201501251200-KBOX-WWUS41-WSWBOX")), boston_map())


@pytest.mark.parametrize("field, value", [("utc_prodissue", "25 Jan 2015"), ("utc_init_expire", "2015-01-28 06:00"),
                                          ("utc_init_expire", "")])
def test_unreadable_times_are_rejected(field, value):
    bad = row()
    bad[field] = value
    with pytest.raises(ValueError, match="unreadable"):
        prepare(raw(bad), boston_map())


def test_the_same_alert_in_two_pulls_counts_once():
    df = raw(row(), row()).assign(pull_year=[2015, 2016])
    alerts, _ = prepare(df, boston_map())
    assert len(alerts) == 1


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_the_same_alert_in_two_pulls_keeps_its_freshest_state(order):
    states = [running(), ended("CAN", "2015-01-26 12:00")]
    alerts, _ = prepare(raw(*(states[i] for i in order)).assign(pull_year=[2015 + i for i in order]), boston_map())
    assert len(alerts) == 1 and alerts["ended_early_utc"].iloc[0] == pd.Timestamp("2015-01-26 12:00", tz="UTC")


def test_a_reissued_alert_keeps_its_second_first_bulletin():
    alerts, _ = prepare(raw(row(), row(first="2015-01-29 10:00", first_end="201501301200")), boston_map())
    assert len(alerts) == 2


def test_a_type_missing_from_the_map_is_kept_as_unmapped():
    alerts, _ = prepare(raw(row(phenomena="QQ", significance="Y")), boston_map())
    assert (alerts["storm_groups"].iloc[0], alerts["alert_type"].iloc[0]) == (UNMAPPED, "QQ.Y")


def test_missing_columns_and_empty_input():
    with pytest.raises(ValueError, match="missing columns"):
        prepare(raw(row()).drop(columns="utc_init_expire"), boston_map())
    with pytest.raises(ValueError, match="missing columns"):
        prepare(raw(row()).drop(columns="status"), boston_map())  # A+ needs the last state to check
    alerts, report = prepare(raw(row(ugc="MAZ014")), boston_map())
    assert alerts.empty and list(alerts.columns) == PREPARED_COLUMNS


def test_eventid_must_be_a_number():
    with pytest.raises(ValueError, match="not a number"):
        prepare(raw(row(eventid="x")), boston_map())


def test_prepare_keeps_only_the_time_an_alert_ended_early():
    cancelled = ended("CAN", "2015-01-26 12:00")
    still_running = running(eventid=2)
    unlike = row(eventid=3, status="CAN", expire="2015-01-26 12:00", updated="2015-01-26 13:00")
    alerts, _ = prepare(raw(cancelled, still_running, unlike), boston_map())
    early = dict(zip(alerts["eventid"], alerts["ended_early_utc"]))
    assert early[1] == pd.Timestamp("2015-01-26 12:00", tz="UTC")
    assert pd.isna(early[2]) and pd.isna(early[3])
    assert not set(LEAKY_COLUMNS) & set(alerts.columns)


# Alert map


def test_repo_map_is_valid_and_maps_every_storm_group():
    df = load_alert_map(MAP)
    groups = {g for value in df["storm_groups"] for g in value.split(";")}
    assert {"snow_ice", "tree", "flooding", "none"} <= groups
    assert not df.duplicated(["phenomena", "significance"]).any()


MAP_HEADER = "phenomena,significance,alert_type,storm_groups,note\n"


@pytest.mark.parametrize(
    "body, message",
    [
        ("WS,W,winter storm warning,hail,\n", "storm_groups"),
        ("WS,W,winter storm warning,snow_ice;snow_ice,\n", "storm_groups"),
        ("WS,W,a,snow_ice,\nWS,W,b,snow_ice,\n", "repeats alert types"),
        ("WS,W,,snow_ice,\n", "blank"),
    ],
)
def test_bad_maps_are_rejected(tmp_path, body, message):
    path = tmp_path / "map.csv"
    path.write_text(MAP_HEADER + body)
    with pytest.raises(ValueError, match=message):
        load_alert_map(path)


def test_map_without_a_column_is_rejected(tmp_path):
    path = tmp_path / "map.csv"
    path.write_text("phenomena,significance,alert_type\nWS,W,winter storm warning\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_alert_map(path)


# Download


def fake_fetch(bodies, calls):
    def fetch(url, dest):
        calls.append(url)
        year = int(url.split("sts=")[1][:4])
        Path(dest).write_text(bodies[year])
    return fetch


def csv_text(*rows):
    return raw(*rows).to_csv(index=False)


def test_a_year_is_kept_once_pulled_two_weeks_after_it_ended(tmp_path):
    raw_dir, state_dir = tmp_path / "nws_alerts", tmp_path / "state"
    raw_dir.mkdir()
    for year in (2015, 2016, 2025):
        write_csv(raw_path(raw_dir, year), row(year=year))
    # 2015 pulled 14 days after it ended; 2025 a minute short of that; 2016 has no record.
    record_pulls(state_dir, {2015: "2016-01-15T00:00:00+00:00", 2025: "2026-01-14T23:59:00+00:00"})
    calls = []
    bodies = {year: csv_text(row(year=year)) for year in (2015, 2016, 2025, 2026)}
    years = [2015, 2016, 2025, 2026]
    report = download_years(raw_dir, years, NOW, fetch=fake_fetch(bodies, calls), state_dir=state_dir)
    assert [r["action"] for r in report] == ["kept", "unchanged", "unchanged", "downloaded"]
    assert calls == [year_url(2016), year_url(2025), year_url(2026)]
    assert json.loads((state_dir / DOWNLOADS).read_text())["2025"]["downloaded_at"] == "2026-10-26T22:30:00+00:00"
    calls.clear()
    again = download_years(raw_dir, years, NOW, fetch=fake_fetch(bodies, calls), state_dir=state_dir)
    assert [r["action"] for r in again] == ["kept", "kept", "kept", "unchanged"]
    assert calls == [year_url(2026)]
    # The pull record lives in the state folder, so raw/ holds the year files and nothing else (#7, #8).
    assert sorted(p.name for p in raw_dir.iterdir()) == [f"box_{year}.csv" for year in years]


def test_an_unchanged_pull_leaves_the_stored_year_as_it_was(tmp_path):
    raw_dir, state_dir = tmp_path / "nws_alerts", tmp_path / "state"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2026), row(year=2026))
    before = raw_path(raw_dir, 2026).stat().st_mtime_ns
    same = download_years(raw_dir, [2026], NOW, fetch=fake_fetch({2026: csv_text(row(year=2026))}, []), state_dir=state_dir)
    assert same[0]["action"] == "unchanged" and raw_path(raw_dir, 2026).stat().st_mtime_ns == before
    assert json.loads((state_dir / DOWNLOADS).read_text())["2026"]["downloaded_at"] == "2026-10-26T22:30:00+00:00"
    newer = csv_text(row(year=2026), row(eventid=2, year=2026))
    changed = download_years(raw_dir, [2026], NOW, fetch=fake_fetch({2026: newer}, []), state_dir=state_dir)
    assert changed[0]["action"] == "updated" and raw_path(raw_dir, 2026).read_text() == newer


def test_refresh_fetches_every_year(tmp_path):
    raw_dir, state_dir = tmp_path / "nws_alerts", tmp_path / "state"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2015), row())
    record_pulls(state_dir, {2015: "2020-01-01T00:00:00+00:00"})
    calls = []
    download_years(raw_dir, [2015], NOW, refresh=True, fetch=fake_fetch({2015: csv_text(row())}, calls), state_dir=state_dir)
    assert calls == [year_url(2015)]


def test_an_error_page_never_replaces_a_good_year(tmp_path):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2026), row(year=2026))
    good = raw_path(raw_dir, 2026).read_text()
    fetch = fake_fetch({2026: "<html><body>Service unavailable</body></html>"}, [])
    with pytest.raises(ValueError, match="missing IEM columns") as failure:
        download_years(raw_dir, [2026], NOW, fetch=fetch, state_dir=tmp_path / "state")
    assert "unchanged" in " ".join(failure.value.__notes__)
    assert raw_path(raw_dir, 2026).read_text() == good
    assert not list(raw_dir.glob("*.download"))


def test_check_raw_rejects_an_empty_file(tmp_path):
    path = tmp_path / "box_2015.csv"
    path.write_text("")
    with pytest.raises(ValueError, match="empty"):
        check_raw(path)


def test_read_raw_names_a_missing_year(tmp_path):
    with pytest.raises(FileNotFoundError, match="--download"):
        read_raw(tmp_path, [2015])


def test_year_url_asks_iem_for_office_box_and_one_year():
    url = year_url(2015)
    assert "watchwarn.py?accept=csv&wfo=BOX" in url
    assert "sts=2015-01-01T00:00Z" in url and "ets=2016-01-01T00:00Z" in url


# New Year (ADR-014): IEM files an alert under the year it starts.


def test_the_next_year_is_pulled_from_15_december():
    assert years_to_pull(2025, date(2026, 12, 14)) == [2025, 2026]
    assert years_to_pull(2025, date(2026, 12, 15)) == [2025, 2026, 2027]
    assert years_to_pull(2025, date(2027, 1, 1)) == [2025, 2026, 2027]


def test_an_alert_sent_on_31_december_that_starts_on_1_january_is_only_in_the_next_years_file(tmp_path):
    write_csv(raw_path(tmp_path, 2026), running(eventid=40, first="2026-12-20 10:00", first_end="202612210000", year=2026))
    write_csv(raw_path(tmp_path, 2027), running(eventid=41, first="2026-12-31 15:00", first_end="202701021200", year=2026))
    new_year = pd.Series(pd.to_datetime(["2027-01-01"]))  # cutoff 2026-12-31 18:00 EST, 23:00 UTC
    years = years_to_pull(2026, date(2026, 12, 31))
    with_next = prepared(read_raw(tmp_path, years))
    without = prepared(read_raw(tmp_path, [2026]))
    assert years == [2026, 2027]
    assert alert_features(with_next, new_year, "18:00", GROUPS).iloc[0]["alert_snow_ice_count"] == 1
    assert alert_features(without, new_year, "18:00", GROUPS).iloc[0]["alert_snow_ice_count"] == 0


def test_in_january_the_previous_year_is_pulled_again_for_its_late_cancellations(tmp_path):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    # Starts 2026-12-30, first announced to end 2027-01-04 12:00 UTC, so IEM files it under 2026.
    alert = dict(eventid=50, first="2026-12-30 15:00", first_end="202701041200", year=2026)
    write_csv(raw_path(raw_dir, 2026), running(**alert))
    write_csv(raw_path(raw_dir, 2027))  # no 2027 alerts yet: IEM's header only
    state_dir = tmp_path / "state"
    record_pulls(state_dir, {2026: "2026-12-31T23:30:00+00:00", 2027: "2026-12-31T23:30:00+00:00"})
    evening = datetime(2027, 1, 2, 23, 30, tzinfo=timezone.utc)  # 18:30 EST on 2 January
    bodies = {2026: csv_text(ended("CAN", "2027-01-02 15:00", **alert)), 2027: csv_text()}
    calls = []
    report = download_years(raw_dir, [2026, 2027], evening, fetch=fake_fetch(bodies, calls), state_dir=state_dir)
    assert calls == [year_url(2026), year_url(2027)]
    assert [r["action"] for r in report] == ["updated", "unchanged"]
    alerts = prepared(read_raw(raw_dir, [2026, 2027]))
    day = alert_features(alerts, pd.Series(pd.to_datetime(["2027-01-03"])), "18:00", GROUPS).iloc[0]
    assert day["alert_snow_ice_count"] == 0  # cancelled at 15:00 UTC, before that evening's 23:00 UTC cutoff


def test_a_next_year_with_no_alerts_yet_may_come_back_empty(tmp_path):
    raw_dir = tmp_path / "nws_alerts"
    december = datetime(2026, 12, 20, 23, 0, tzinfo=timezone.utc)
    bodies = {2026: csv_text(row(year=2026)), 2027: ""}
    state_dir = tmp_path / "state"
    report = download_years(raw_dir, [2026, 2027], december, fetch=fake_fetch(bodies, []), state_dir=state_dir)
    assert [r["action"] for r in report] == ["downloaded", "no alerts yet"]
    assert not raw_path(raw_dir, 2027).exists() and not list(raw_dir.glob("*.download"))
    assert len(read_raw(raw_dir, [2026, 2027], optional={2027})) == 1
    with pytest.raises(ValueError, match="empty"):  # an empty answer for this year stays an error
        download_years(raw_dir, [2026], december, fetch=fake_fetch({2026: ""}, []), state_dir=state_dir)
    assert check_raw(raw_path(raw_dir, 2026)) == 1


# Cutoff and features


def test_cutoff_is_the_evening_before_in_boston_time():
    days = pd.DatetimeIndex(["2015-01-27", "2015-07-15"])
    assert list(cutoff_utc(days, "18:00")) == [pd.Timestamp("2015-01-26 23:00", tz="UTC"),   # EST
                                               pd.Timestamp("2015-07-14 22:00", tz="UTC")]  # EDT
    assert list(day_start_utc(days)) == [pd.Timestamp("2015-01-27 05:00", tz="UTC"),
                                         pd.Timestamp("2015-07-15 04:00", tz="UTC")]


def test_cutoff_and_day_start_on_daylight_saving_change_days():
    days = pd.DatetimeIndex(["2015-03-08", "2015-03-09", "2015-11-01", "2015-11-02"])
    cutoffs = ["2015-03-07 23:00", "2015-03-08 22:00", "2015-10-31 22:00", "2015-11-01 23:00"]
    starts = ["2015-03-08 05:00", "2015-03-09 04:00", "2015-11-01 04:00", "2015-11-02 05:00"]
    assert list(cutoff_utc(days, "18:00")) == [pd.Timestamp(t, tz="UTC") for t in cutoffs]
    assert list(day_start_utc(days)) == [pd.Timestamp(t, tz="UTC") for t in starts]


def features_for(alert_rows, day="2015-01-27", cutoff="18:00", begins=None):
    table = alert_features(prepared(raw(*alert_rows), begins), pd.Series(pd.to_datetime([day])), cutoff, GROUPS)
    return table.iloc[0]


def test_an_alert_counts_only_if_first_sent_before_the_cutoff():
    # 2015-01-26 18:00 Boston (EST) is 23:00 UTC.
    before = features_for([row(first="2015-01-26 22:59", first_end="201501280600")])
    after = features_for([row(first="2015-01-26 23:01", first_end="201501280600")])
    assert (before["alert_snow_ice_count"], before["alert_snow_ice_warning"]) == (1, 1)
    assert (after["alert_snow_ice_count"], after["alert_snow_ice_warning"]) == (0, 0)


def test_an_alert_that_ends_before_the_day_begins_does_not_count():
    # 2015-01-27 starts at 05:00 UTC; this alert was first announced to end at 04:59 UTC.
    assert features_for([row(first="2015-01-26 10:00", first_end="201501270459")])["alert_snow_ice_count"] == 0
    assert features_for([row(first="2015-01-26 10:00", first_end="201501270501")])["alert_snow_ice_count"] == 1


def test_later_bulletins_after_the_cutoff_cannot_leak():
    # An alert first sent after the cutoff is invisible for that day, whatever its times say.
    base = [row(first="2015-01-26 12:00", first_end="201501280600")]
    leaked = base + [row(phenomena="HW", eventid=9, first="2015-01-27 01:00", first_end="201501280000")]
    pd.testing.assert_series_equal(features_for(base), features_for(leaked))


def test_an_alert_for_zone_and_county_counts_once_and_watches_are_not_warnings():
    watch = row(significance="A")
    both = features_for([watch, {**watch, "ugc": "MAC025"}])
    assert (both["alert_snow_ice_count"], both["alert_snow_ice_warning"]) == (1, 0)


def test_tree_and_flooding_alerts_count_in_both_groups():
    storm = features_for([row(phenomena="TR", significance="W")])
    assert (storm["alert_tree_count"], storm["alert_flooding_count"], storm["alert_snow_ice_count"]) == (1, 1, 0)


def test_a_warning_sent_when_it_starts_is_never_known_the_evening_before():
    # County severe thunderstorm warnings start when first sent (ADR-014): 3 pm on the day itself.
    storm = features_for([row(ugc="MAC025", phenomena="SV", first="2015-01-27 20:00", first_end="201501272100")])
    assert storm["alert_tree_count"] == 0 and storm["alert_flooding_count"] == 0


def test_none_and_unmapped_alerts_feed_no_group():
    quiet = features_for([row(phenomena="HT", significance="Y"), row(phenomena="QQ", significance="Y", eventid=3)])
    assert all(quiet[f"alert_{g}_count"] == 0 for g in GROUPS)


def test_no_alerts_give_zeros_for_every_day():
    alerts = prepared(raw(row(ugc="MAZ014")))
    table = alert_features(alerts, pd.Series(pd.date_range("2015-01-01", periods=3)), "18:00", GROUPS)
    assert len(table) == 3 and (table.filter(like="alert_") == 0).all().all()


# A+ (ADR-014). For 2015-01-27 the cutoff is 2015-01-26 18:00 EST, 23:00 UTC; the day begins at 05:00 UTC.


@pytest.mark.parametrize("cancelled, count", [("2015-01-26 22:59", 0), ("2015-01-26 23:00", 1), ("2015-01-27 02:00", 1)])
def test_a_cancellation_counts_only_before_the_cutoff(cancelled, count):
    day = features_for([ended("CAN", cancelled)])
    assert (day["alert_snow_ice_count"], day["alert_snow_ice_warning"]) == (count, count)
    if count:  # at or after the cutoff it changes nothing: the evening run could not have seen it
        pd.testing.assert_series_equal(day, features_for([running()]))


def test_an_upgrade_before_the_cutoff_replaces_the_watch_with_the_warning():
    upgraded = features_for([ended("UPG", "2015-01-26 15:00", significance="A"), running(eventid=2, first="2015-01-26 15:00")])
    assert (upgraded["alert_snow_ice_count"], upgraded["alert_snow_ice_warning"]) == (1, 1)
    # Upgraded after the cutoff: the watch still counts and the warning was not known yet.
    late = features_for([ended("UPG", "2015-01-26 23:30", significance="A"), running(eventid=2, first="2015-01-26 23:30")])
    assert (late["alert_snow_ice_count"], late["alert_snow_ice_warning"]) == (1, 0)


@pytest.mark.parametrize("status, expire, updated", [
    ("CAN", "2015-01-26 12:00", "2015-01-26 13:00"),  # utc_expire unlike utc_updated
    ("UPG", "", ""),                                  # no times
    ("EXP", "2015-01-26 12:00", "2015-01-26 12:00"),  # end shortened, then expired: neither CAN nor UPG
    ("CON", "2015-01-26 12:00", "2015-01-26 12:00"),  # end shortened
])
def test_without_all_three_conditions_an_alert_keeps_its_first_announced_end(status, expire, updated):
    assert features_for([row(status=status, expire=expire, updated=updated)])["alert_snow_ice_count"] == 1


def test_an_extension_is_not_used():
    # Extended before the cutoff to 2015-01-29 12:00 UTC; first announced to end 2015-01-28 06:00 UTC.
    extended = row(status="EXT", expire="2015-01-29 12:00", updated="2015-01-26 12:00")
    assert features_for([extended], day="2015-01-28")["alert_snow_ice_count"] == 1  # the day begins 05:00 UTC
    assert features_for([extended], day="2015-01-29")["alert_snow_ice_count"] == 0


def test_one_event_for_zone_and_county_counts_once_while_either_area_runs():
    zone = running()
    county_later = running(ugc="MAC025", first="2015-01-26 03:00")  # added later, with its own first bulletin
    county_cancelled = ended("CAN", "2015-01-26 12:00", ugc="MAC025", first="2015-01-26 03:00")
    assert features_for([zone, county_later])["alert_snow_ice_count"] == 1
    assert features_for([zone, county_cancelled])["alert_snow_ice_count"] == 1  # the zone still runs
    assert features_for([ended("CAN", "2015-01-26 12:00"), county_cancelled])["alert_snow_ice_count"] == 0
    assert features_for([zone, running(ugc="MAC025", eventid=2)])["alert_snow_ice_count"] == 2  # two events


@pytest.mark.parametrize("day, cancelled, count", [
    ("2015-03-09", "2015-03-08 21:59", 0),  # 18:00 EDT on 8 March is 22:00 UTC
    ("2015-03-09", "2015-03-08 22:30", 1),
    ("2015-11-02", "2015-11-01 22:30", 0),  # 18:00 EST on 1 November is 23:00 UTC
    ("2015-11-02", "2015-11-01 23:00", 1),
])
def test_the_cancellation_cutoff_follows_daylight_saving_time(day, cancelled, count):
    first = (pd.Timestamp(day) - pd.Timedelta(days=2)).strftime("%Y-%m-%d 12:00")
    first_end = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y%m%d1200")
    alert = ended("CAN", cancelled, first=first, first_end=first_end)
    assert features_for([alert], day=day)["alert_snow_ice_count"] == count


def test_history_and_the_evening_run_give_the_same_features():
    # The pull made just after the cutoff for 2015-01-27, and one made long after, once more updates came in.
    wind = dict(phenomena="HW", eventid=3, first="2015-01-26 22:00", first_end="201501271200")
    evening = [running(), ended("CAN", "2015-01-26 20:00", eventid=2), running(**wind)]
    later = [
        ended("CAN", "2015-01-27 02:00"),  # cancelled after the cutoff
        evening[1],  # cancelled before it, unchanged
        row(**wind, status="EXT", expire="2015-01-28 12:00", updated="2015-01-27 09:00"),  # extended after it
        running(eventid=4, first="2015-01-26 23:30"),  # first sent after it
    ]
    tonight = features_for(evening)
    assert (tonight["alert_snow_ice_count"], tonight["alert_tree_count"]) == (1, 1)
    pd.testing.assert_series_equal(tonight, features_for(later))


def test_features_are_the_same_after_a_parquet_round_trip(tmp_path):
    alerts = prepared(raw(running(), ended("CAN", "2015-01-26 12:00", eventid=2), running(ugc="MAC025")),
                      begins={2: "2015-01-26 05:00"})
    alerts.to_parquet(tmp_path / "alerts.parquet", index=False)
    back = pd.read_parquet(tmp_path / "alerts.parquet")
    days = pd.Series(pd.date_range("2015-01-25", periods=5))
    pd.testing.assert_frame_equal(alert_features(alerts, days, "18:00", GROUPS), alert_features(back, days, "18:00", GROUPS))


def test_alerts_without_the_a_plus_column_are_rejected():
    alerts = prepared(raw(row()))
    with pytest.raises(ValueError, match="ended_early_utc"):
        alert_features(alerts.drop(columns="ended_early_utc"), pd.Series(pd.to_datetime(["2015-01-27"])), "18:00", GROUPS)


def test_alerts_without_a_first_announced_begin_are_rejected():
    day = pd.Series(pd.to_datetime(["2015-01-27"]))
    alerts = prepared(raw(row()))
    with pytest.raises(ValueError, match="first_begin_utc"):  # a parquet written before #107
        alert_features(alerts.drop(columns="first_begin_utc"), day, "18:00", GROUPS)
    with pytest.raises(ValueError, match="no first-announced begin"):  # never treated as "no alert"
        alert_features(alerts.assign(first_begin_utc=pd.NaT), day, "18:00", GROUPS)


# Begins after D (#107, the CP1 leak): an alert first announced to begin after D does not count for D.
# For 2015-01-27 the day ends at 05:00 UTC on 28 January (midnight EST); for 2015-07-15 at 04:00 UTC (EDT).


@pytest.mark.parametrize("day, first, begin, count", [
    ("2015-01-27", "2015-01-26 12:00", "2015-01-28 04:59", 1),
    ("2015-01-27", "2015-01-26 12:00", "2015-01-28 05:00", 0),
    ("2015-07-15", "2015-07-14 12:00", "2015-07-16 03:59", 1),
    ("2015-07-15", "2015-07-14 12:00", "2015-07-16 04:00", 0),
])
def test_an_alert_announced_to_begin_after_the_day_does_not_count_for_it(day, first, begin, count):
    end = (pd.Timestamp(day) + pd.Timedelta(days=3)).strftime("%Y%m%d1200")
    watch = running(significance="A", first=first, first_end=end)
    assert features_for([watch], day=day, begins={1: begin})["alert_snow_ice_count"] == count


def test_a_watch_sent_days_ahead_counts_for_the_storm_day_only():
    # Sent 25 January, first announced to begin 28 January 06:00 UTC (01:00 EST) and end 29 January 12:00 UTC.
    # Before #107 it counted for 26 and 27 January too, since only its end was compared with the day.
    watch = running(significance="A", first="2015-01-25 20:58", first_end="201501291200")
    days = ["2015-01-26", "2015-01-27", "2015-01-28"]
    counts = [features_for([watch], day=day, begins={1: "2015-01-28 06:00"})["alert_snow_ice_count"] for day in days]
    assert counts == [0, 0, 1]


# First-announced begin (#107): read from the alert's VTEC string in its first bulletin, in Boston's segment.

SENT = pd.Timestamp("2015-01-25 20:58", tz="UTC")
# The 2015 blizzard warning's first bulletin, trimmed (IEM text service; NWS, public domain).
BLIZZARD = """041
WWUS41 KBOX 252058
WSWBOX

URGENT - WINTER WEATHER MESSAGE
NATIONAL WEATHER SERVICE TAUNTON MA
358 PM EST SUN JAN 25 2015

...A CRIPPLING AND POTENTIALLY HISTORIC BLIZZARD TO IMPACT THE
AREA MAINLY FROM LATE MONDAY INTO TUESDAY...LINGERING INTO EARLY
WEDNESDAY...

MAZ005>007-013>021-RIZ001>008-260500-
/O.UPG.KBOX.BZ.A.0001.150127T0000Z-150128T0600Z/
/O.NEW.KBOX.BZ.W.0001.150127T0000Z-150128T0600Z/
CENTRAL MIDDLESEX MA-WESTERN ESSEX MA-EASTERN ESSEX MA-
WESTERN NORFOLK MA-SOUTHEAST MIDDLESEX MA-SUFFOLK MA-
358 PM EST SUN JAN 25 2015

$$

MAZ022-023-260500-
/O.UPG.KBOX.WS.A.0003.150127T0000Z-150128T0600Z/
/O.NEW.KBOX.BZ.W.0001.150127T0000Z-150127T1700Z/
BARNSTABLE MA-DUKES MA-
358 PM EST SUN JAN 25 2015

$$

MAZ002>004-008>011-026-260500-
/O.UPG.KBOX.WS.A.0003.150127T0000Z-150128T0600Z/
/O.NEW.KBOX.WS.W.0003.150127T0000Z-150128T0600Z/
NORTHWEST MIDDLESEX MA-NORTH CENTRAL WORCESTER MA-
358 PM EST SUN JAN 25 2015

$$
"""


@pytest.mark.parametrize("block, codes", [
    ("MAZ015-260500-", {"MAZ015"}),
    ("MAZ005>007-013-RIZ001>002-260500-", {"MAZ005", "MAZ006", "MAZ007", "MAZ013", "RIZ001", "RIZ002"}),
    ("MAC021-025-252230-", {"MAC021", "MAC025"}),
    ("MAZ013>MAZ015-260500-", {"MAZ013", "MAZ014", "MAZ015"}),
])
def test_ugc_codes_expand_ranges_and_keep_the_state_and_type(block, codes):
    assert ugc_codes(block) == codes


def test_a_ugc_number_without_a_state_before_it_is_unreadable():
    with pytest.raises(ValueError, match="unreadable UGC"):
        ugc_codes("015-260500-")


def test_the_begin_comes_from_the_segment_that_names_the_area():
    assert first_begin(BLIZZARD, "MAZ015", "BZ", "W", 1, SENT) == (pd.Timestamp("2015-01-27 00:00", tz="UTC"), True)
    # WS.W 3 went only to other zones, so Boston's bulletin has no begin for it and the run stops.
    with pytest.raises(ValueError, match="0 begins for WS.W 3 in MAZ015"):
        first_begin(BLIZZARD, "MAZ015", "WS", "W", 3, SENT)


def test_each_area_takes_its_own_segments_begin():
    text = bulletin_text(("MAZ015-260500-", "/O.NEW.KBOX.WS.W.0004.150127T1200Z-150128T0600Z/"),
                         ("MAZ016>017-260500-", "/O.NEW.KBOX.WS.W.0004.150127T0000Z-150128T0600Z/"))
    assert first_begin(text, "MAZ015", "WS", "W", 4, SENT)[0] == pd.Timestamp("2015-01-27 12:00", tz="UTC")
    assert first_begin(text, "MAZ016", "WS", "W", 4, SENT)[0] == pd.Timestamp("2015-01-27 00:00", tz="UTC")


def test_a_begin_of_zeros_means_the_alert_began_when_sent():
    text = bulletin_text(("MAZ015-252300-", "/O.EXA.KBOX.WW.Y.0005.000000T0000Z-150125T2300Z/"))
    assert first_begin(text, "MAZ015", "WW", "Y", 5, SENT) == (SENT, True)


def test_a_ugc_block_can_run_over_several_lines():
    text = bulletin_text(("CTZ002>004-MAZ002>004-008>012-\n014>016-RIZ001-260500-",
                          "/O.NEW.KBOX.WW.Y.0002.150126T0300Z-150126T1500Z/"))
    assert first_begin(text, "MAZ015", "WW", "Y", 2, SENT)[0] == pd.Timestamp("2015-01-26 03:00", tz="UTC")


def test_an_event_iem_renumbered_takes_the_one_alert_of_its_type_the_segment_starts():
    # IEM records this wind chill advisory as WC.Y 4; its first bulletin says WC.Y 1 (ADR-014, December 2017).
    text = bulletin_text(("MAZ002-008-009-271200-", "/O.NEW.KBOX.WC.W.0001.171228T0000Z-171228T1700Z/"),
                         ("MAZ003>007-012>024-271200-", "/O.NEW.KBOX.WC.Y.0001.171228T0000Z-171228T1700Z/"))
    assert first_begin(text, "MAZ015", "WC", "Y", 4, SENT) == (pd.Timestamp("2017-12-28 00:00", tz="UTC"), False)
    two = bulletin_text(("MAZ015-271200-", "/O.NEW.KBOX.WC.Y.0001.171228T0000Z-171228T1700Z/\n"
                                           "/O.NEW.KBOX.WC.Y.0002.171229T0000Z-171229T1700Z/"))
    continued = bulletin_text(("MAZ015-271200-", "/O.CON.KBOX.WC.Y.0001.171228T0000Z-171228T1700Z/"))
    for text in (two, continued):  # two of the type, or one the segment does not start: no way to tell
        with pytest.raises(ValueError, match="0 begins"):
            first_begin(text, "MAZ015", "WC", "Y", 4, SENT)


def test_with_first_begins_reads_each_alerts_own_bulletin(tmp_path):
    alerts, _ = prepare(raw(row(), row(ugc="MAC025", phenomena="SV", eventid=7)), boston_map())
    write_bulletin(tmp_path, "201501252058-KBOX-WWUS41-WSWBOX",
                   ("MAZ015-260500-", "/O.NEW.KBOX.WS.W.0001.150127T0000Z-150128T0600Z/"),
                   ("MAC025-252100-", "/O.NEW.KBOX.SV.W.0007.000000T0000Z-150125T2100Z/"))
    begun, by_type = with_first_begins(alerts, lambda product_id: read_bulletin(tmp_path, product_id))
    assert list(begun.columns) == OUT_COLUMNS and not set(LEAKY_COLUMNS) & set(begun.columns)
    assert dict(zip(begun["phenomena"], begun["first_begin_utc"])) == {
        "WS": pd.Timestamp("2015-01-27 00:00", tz="UTC"), "SV": SENT}
    assert by_type == []
    with pytest.raises(FileNotFoundError, match="--download"):
        with_first_begins(alerts, lambda product_id: read_bulletin(tmp_path / "elsewhere", product_id))


def test_with_first_begins_lists_renumbered_alerts_and_names_a_bulletin_it_cannot_read(tmp_path):
    pid = "201501252058-KBOX-WWUS41-WSWBOX"
    alerts, _ = prepare(raw(row(phenomena="WC", significance="Y", eventid=4)), boston_map())
    write_bulletin(tmp_path, pid, ("MAZ015-260500-", "/O.NEW.KBOX.WC.Y.0001.150126T0000Z-150126T1200Z/"))
    begun, by_type = with_first_begins(alerts, lambda product_id: read_bulletin(tmp_path, product_id))
    assert by_type == [f"WC.Y 4 in {pid}"] and begun["first_begin_utc"].iloc[0] == pd.Timestamp("2015-01-26", tz="UTC")
    write_bulletin(tmp_path, pid, ("MAZ016-260500-", "/O.NEW.KBOX.WC.Y.0004.150126T0000Z-150126T1200Z/"))
    with pytest.raises(ValueError, match="0 begins") as failure:  # sent to another zone only
        with_first_begins(alerts, lambda product_id: read_bulletin(tmp_path, product_id))
    assert f"in bulletin {pid}" in " ".join(failure.value.__notes__)


def fake_texts(texts, calls):
    def fetch(url, dest):
        calls.append(url)
        Path(dest).write_text(texts[url.rsplit("/", 1)[1].split("?")[0]], encoding="utf-8")
    return fetch


def test_download_bulletins_fetches_each_missing_one_once(tmp_path):
    ids = ["201501252058-KBOX-WWUS41-WSWBOX", "202012251926-KBOX-WGUS41-FLWBOX-RRA"]
    texts = {pid: bulletin_text(("MAZ015-260500-", "/O.NEW.KBOX.WS.W.0001.150127T0000Z-150128T0600Z/")) for pid in ids}
    calls = []
    report = download_bulletins(tmp_path, [*ids, ids[0]], fetch=fake_texts(texts, calls), pause_s=0)
    assert report == {"needed": 2, "already stored": 0, "downloaded": 2}
    assert calls == [bulletin_url(pid) for pid in ids] and calls[0].endswith("?nolimit=true")
    assert read_bulletin(tmp_path, ids[1]) == texts[ids[1]]
    calls.clear()  # a bulletin never changes, so a stored one is never fetched again
    assert download_bulletins(tmp_path, ids, fetch=fake_texts(texts, calls), pause_s=0)["downloaded"] == 0
    assert calls == []


def test_an_error_page_never_becomes_a_bulletin(tmp_path):
    pid = "201501252058-KBOX-WWUS41-WSWBOX"
    with pytest.raises(ValueError, match="no VTEC string") as failure:
        download_bulletins(tmp_path, [pid], fetch=fake_texts({pid: '{"detail": "Not Found"}'}, []), pause_s=0)
    assert "nothing was stored" in " ".join(failure.value.__notes__)
    assert not bulletin_path(tmp_path, pid).exists() and not list((tmp_path / "bulletins").glob("*.download"))


def test_a_product_id_must_look_like_iems(tmp_path):
    with pytest.raises(ValueError, match="not an IEM product id"):
        bulletin_path(tmp_path, "../../outside")


# Freshness (ADR-014): the evening before 2015-01-27, whose cutoff is 23:00 UTC.

CUTOFF = pd.Timestamp("2015-01-26 23:00", tz="UTC")
PULLED = pd.Timestamp("2015-01-26 23:30", tz="UTC")
WS_W_1 = "/O.NEW.KBOX.WS.W.0001.150127T0000Z-150128T0600Z/"


def message(vtec, sent="2015-01-25T15:58:00-05:00", ugcs=("MAZ015",), status="Actual"):
    return {"properties": {"status": status, "sent": sent, "geocode": {"UGC": list(ugcs)}, "parameters": {"VTEC": [vtec]}}}


def snapshot(*messages, updated="2015-01-26T23:20:00+00:00"):
    return {"type": "FeatureCollection", "updated": updated, "features": list(messages)}


def test_freshness_passes_when_every_message_before_the_cutoff_is_in_the_pull():
    fresh = check_freshness(raw(running()), snapshot(message(WS_W_1)), CUTOFF, PULLED)
    assert fresh["messages"] == 1
    assert fresh["lag_at_most_minutes"] == 1592  # the pull minus IEM's newest update, 2015-01-25 20:58 UTC


def test_freshness_fails_when_a_message_before_the_cutoff_is_missing():
    missing = message("/O.NEW.KBOX.WS.W.0002.150127T0600Z-150128T0600Z/", sent="2015-01-26T17:59:00-05:00")
    with pytest.raises(FreshnessError, match=r"WS\.W\.0002.*not in the pull"):
        check_freshness(raw(running()), snapshot(message(WS_W_1), missing), CUTOFF, PULLED)


def test_freshness_skips_late_test_drill_and_other_area_messages():
    later = "/O.NEW.KBOX.WS.W.0002.150127T0600Z-150128T0600Z/"
    at_cutoff = message(later, sent="2015-01-26T18:00:00-05:00")
    elsewhere = message("/O.NEW.KBOX.WS.W.0003.150127T0600Z-150128T0600Z/", ugcs=("MAZ014",))
    test = message("/T.NEW.KBOX.WS.W.0004.150127T0600Z-150128T0600Z/")
    drill = message("/O.NEW.KBOX.WS.W.0005.150127T0600Z-150128T0600Z/", status="Exercise")
    assert check_freshness(raw(running()), snapshot(at_cutoff, elsewhere, test, drill), CUTOFF, PULLED)["messages"] == 0


def test_freshness_fails_until_an_upgrade_before_the_cutoff_reaches_iem():
    upgrade = message("/O.UPG.KBOX.WS.A.0001.000000T0000Z-150128T0600Z/", sent="2015-01-26T17:00:00-05:00")
    with pytest.raises(FreshnessError, match="without its UPG"):
        check_freshness(raw(running(significance="A")), snapshot(upgrade), CUTOFF, PULLED)
    upgraded = ended("UPG", "2015-01-26 22:00", significance="A")
    assert check_freshness(raw(upgraded), snapshot(upgrade), CUTOFF, PULLED)["messages"] == 1


def test_freshness_fails_on_a_pull_or_snapshot_older_than_the_cutoff():
    with pytest.raises(FreshnessError, match="IEM pull"):
        check_freshness(raw(running()), snapshot(), CUTOFF, pd.Timestamp("2015-01-26 22:59", tz="UTC"))
    with pytest.raises(FreshnessError, match="NWS snapshot"):
        check_freshness(raw(running()), snapshot(updated="2015-01-26T17:59:00-05:00"), CUTOFF, PULLED)


# Command line


def test_main_prepares_the_pulled_years(tmp_path, monkeypatch, capsys):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2015), row(), row(ugc="MAC025", phenomena="SV", eventid=7), row(ugc="MAZ014"))
    out = tmp_path / "alerts.parquet"
    argv = ["alert_history.py", "--raw-dir", str(raw_dir), "--map", str(MAP), "--out", str(out),
            "--first-year", "2015", "--last-year", "2015"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(FileNotFoundError, match="--download"):  # no first bulletins stored yet
        alert_history.main()
    write_bulletin(raw_dir, "201501252058-KBOX-WWUS41-WSWBOX",
                   ("MAZ015-260500-", "/O.NEW.KBOX.WS.W.0001.150127T0000Z-150128T0600Z/"),
                   ("MAC025-252100-", "/O.NEW.KBOX.SV.W.0007.000000T0000Z-150125T2100Z/"))
    alert_history.main()
    written = pd.read_parquet(out)
    assert len(written) == 2 and not set(LEAKY_COLUMNS) & set(written.columns)
    assert sorted(written["first_begin_utc"]) == [SENT, pd.Timestamp("2015-01-27 00:00", tz="UTC")]
    printed = capsys.readouterr().out
    assert "| 2015 | 1 | 1 |" in printed and "WARNING" not in printed
    assert "1 of 2 alerts were first announced to begin after their first bulletin" in printed


def test_main_checks_freshness_before_it_writes(tmp_path, monkeypatch, capsys):
    raw_dir, state_dir, nws_dir = tmp_path / "nws_alerts", tmp_path / "state", tmp_path / "nws"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2015), running())
    write_bulletin(raw_dir, "201501252058-KBOX-WWUS41-WSWBOX", ("MAZ015-260500-", WS_W_1))
    record_pulls(state_dir, {2015: "2015-01-26T23:30:00+00:00"})
    folder = nws_dir / "2015-01-26"
    folder.mkdir(parents=True)
    (nws_dir / "_manifest.json").write_text("{}")  # not a snapshot
    (folder / "20150126T232000Z.json").write_text(json.dumps(snapshot(message(WS_W_1))))
    out = tmp_path / "alerts.parquet"
    argv = ["alert_history.py", "--raw-dir", str(raw_dir), "--state-dir", str(state_dir), "--map", str(MAP),
            "--out", str(out), "--first-year", "2015", "--last-year", "2015", "--check-nws", str(nws_dir),
            "--settings", str(SETTINGS), "--today", "2015-01-26"]
    monkeypatch.setattr(sys, "argv", argv)
    alert_history.main()
    assert out.exists() and "freshness: the 1 Boston NWS message(s)" in capsys.readouterr().out

    out.unlink()
    newer = snapshot(message(WS_W_1), message("/O.NEW.KBOX.WS.W.0002.150127T0600Z-150128T0600Z/"),
                     updated="2015-01-26T23:30:00+00:00")
    (folder / "20150126T233000Z.json").write_text(json.dumps(newer))
    with pytest.raises(FreshnessError, match=r"WS\.W\.0002"):
        alert_history.main()
    assert not out.exists()
