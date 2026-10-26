import sys
from pathlib import Path

import pandas as pd
import pytest

import alert_history
from alert_history import (
    LEAKY_COLUMNS,
    OUT_COLUMNS,
    UNMAPPED,
    alert_features,
    check_raw,
    cutoff_utc,
    day_start_utc,
    download_years,
    load_alert_map,
    prepare,
    raw_path,
    read_raw,
    year_url,
)

REPO = Path(__file__).resolve().parents[2]
MAP = REPO / "docs" / "data" / "alert-map.csv"
GROUPS = ["snow_ice", "tree", "flooding"]
HEADER = ["wfo", "utc_issue", "utc_expire", "utc_prodissue", "utc_init_expire", "phenomena", "gtype",
          "significance", "eventid", "status", "ugc", "utc_updated", "product_id", "vtec_year"]


def row(ugc="MAZ015", phenomena="WS", significance="W", eventid=1, first="2015-01-25 20:58",
        first_end="201501280600", year=2015, product_id=None):
    stamp = pd.Timestamp(first).strftime("%Y%m%d%H%M")
    return {
        "wfo": "BOX", "utc_issue": "2015-01-27 00:00", "utc_expire": "2015-01-27 23:37",
        "utc_prodissue": first, "utc_init_expire": first_end, "phenomena": phenomena, "gtype": "C",
        "significance": significance, "eventid": str(eventid), "status": "CAN", "ugc": ugc,
        "utc_updated": "2015-01-27 23:37", "product_id": product_id or f"{stamp}-KBOX-WWUS41-WSWBOX",
        "vtec_year": str(year),
    }


def raw(*rows):
    return pd.DataFrame(list(rows), columns=HEADER).astype(str)


def boston_map():
    return load_alert_map(MAP)


def write_csv(path, *rows):
    raw(*rows).to_csv(path, index=False)


# Prepare


def test_prepare_keeps_boston_rows_and_only_safe_fields():
    alerts, report = prepare(raw(row(), row(ugc="MAC025", phenomena="SV", eventid=7), row(ugc="MAZ014"), row(ugc="")),
                             boston_map())
    assert list(alerts.columns) == OUT_COLUMNS
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


def test_a_reissued_alert_keeps_its_second_first_bulletin():
    alerts, _ = prepare(raw(row(), row(first="2015-01-29 10:00", first_end="201501301200")), boston_map())
    assert len(alerts) == 2


def test_a_type_missing_from_the_map_is_kept_as_unmapped():
    alerts, _ = prepare(raw(row(phenomena="QQ", significance="Y")), boston_map())
    assert (alerts["storm_groups"].iloc[0], alerts["alert_type"].iloc[0]) == (UNMAPPED, "QQ.Y")


def test_missing_columns_and_empty_input():
    with pytest.raises(ValueError, match="missing columns"):
        prepare(raw(row()).drop(columns="utc_init_expire"), boston_map())
    alerts, report = prepare(raw(row(ugc="MAZ014")), boston_map())
    assert alerts.empty and list(alerts.columns) == OUT_COLUMNS


def test_eventid_must_be_a_number():
    with pytest.raises(ValueError, match="not a number"):
        prepare(raw(row(eventid="x")), boston_map())


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


def test_finished_years_are_kept_and_the_current_year_is_fetched(tmp_path):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2015), row())
    calls = []
    bodies = {2015: csv_text(row()), 2016: csv_text(row(year=2016))}
    report = download_years(raw_dir, [2015, 2016], current_year=2016, fetch=fake_fetch(bodies, calls))
    assert [r["action"] for r in report] == ["kept", "downloaded"]
    assert calls == [year_url(2016)]
    assert raw_path(raw_dir, 2016).exists()


def test_refresh_fetches_every_year(tmp_path):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2015), row())
    calls = []
    download_years(raw_dir, [2015], current_year=2026, refresh=True, fetch=fake_fetch({2015: csv_text(row())}, calls))
    assert calls == [year_url(2015)]


def test_an_error_page_never_replaces_a_good_year(tmp_path):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2026), row(year=2026))
    good = raw_path(raw_dir, 2026).read_text()
    fetch = fake_fetch({2026: "<html><body>Service unavailable</body></html>"}, [])
    with pytest.raises(ValueError, match="missing IEM columns"):
        download_years(raw_dir, [2026], current_year=2026, fetch=fetch)
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


# Cutoff and features


def test_cutoff_is_the_evening_before_in_boston_time():
    days = pd.DatetimeIndex(["2015-01-27", "2015-07-15"])
    assert list(cutoff_utc(days, "18:00")) == [pd.Timestamp("2015-01-26 23:00", tz="UTC"),   # EST
                                               pd.Timestamp("2015-07-14 22:00", tz="UTC")]  # EDT
    assert list(day_start_utc(days)) == [pd.Timestamp("2015-01-27 05:00", tz="UTC"),
                                         pd.Timestamp("2015-07-15 04:00", tz="UTC")]


def features_for(alert_rows, day="2015-01-27", cutoff="18:00"):
    alerts, _ = prepare(raw(*alert_rows), boston_map())
    table = alert_features(alerts, pd.Series(pd.to_datetime([day])), cutoff, GROUPS)
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
    alerts, _ = prepare(raw(row(ugc="MAZ014")), boston_map())
    table = alert_features(alerts, pd.Series(pd.date_range("2015-01-01", periods=3)), "18:00", GROUPS)
    assert len(table) == 3 and (table.filter(like="alert_") == 0).all().all()


# Command line


def test_main_prepares_the_pulled_years(tmp_path, monkeypatch, capsys):
    raw_dir = tmp_path / "nws_alerts"
    raw_dir.mkdir()
    write_csv(raw_path(raw_dir, 2015), row(), row(ugc="MAC025", phenomena="SV", eventid=7), row(ugc="MAZ014"))
    out = tmp_path / "alerts.parquet"
    argv = ["alert_history.py", "--raw-dir", str(raw_dir), "--map", str(MAP), "--out", str(out),
            "--first-year", "2015", "--last-year", "2015"]
    monkeypatch.setattr(sys, "argv", argv)
    alert_history.main()
    written = pd.read_parquet(out)
    assert len(written) == 2 and not set(LEAKY_COLUMNS) & set(written.columns)
    printed = capsys.readouterr().out
    assert "| 2015 | 1 | 1 |" in printed and "WARNING" not in printed
