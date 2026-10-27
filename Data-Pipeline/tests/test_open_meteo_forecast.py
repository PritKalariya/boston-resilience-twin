import json
import logging
from datetime import date

import pandas as pd
import pytest

from alert_history import cutoff_utc
from open_meteo_forecast import DAYS_BEFORE, TIME_FORMAT, VALUES, columns, daily_forecasts, read_hourly, years_for

UNITS = {f"{name}_previous_day{n}": unit for name, unit, *_ in VALUES.values() for n in DAYS_BEFORE}
TODAY = date(2026, 10, 26)
WINTER = "2025-01-15"  # EST: cutoff 2025-01-14 23:00 UTC; hours stamped 2025-01-15 06:00 to 2025-01-16 05:00 UTC
SUMMER = "2025-07-15"  # EDT: cutoff 2025-07-14 22:00 UTC


def answer(start, end, value=lambda column, hour: 0.0, units=None, offset=0, time_format=TIME_FORMAT):
    """A Previous Runs API answer as #8 stores it: every hour from start 00:00 to end 23:00 UTC."""
    hours = pd.date_range(f"{start} 00:00", f"{end} 23:00", freq="h")
    return {
        "latitude": 42.36, "longitude": -71.01, "utc_offset_seconds": offset, "timezone": "GMT",
        "hourly_units": {"time": "iso8601", **(UNITS if units is None else units)},
        "hourly": {"time": [hour.strftime(time_format) for hour in hours],
                   **{column: [value(column, hour) for hour in hours] for column in columns()}},
    }


def write(folder, year, body):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{year}.json").write_text(json.dumps(body))
    return folder


def by_value(**rules):
    """value(column, hour) from rules per Open-Meteo value: name -> f(days_before, hour); 0 elsewhere."""
    def value(column, hour):
        name, n = column.rsplit("_previous_day", 1)
        return rules[name](int(n), hour) if name in rules else 0.0
    return value


def gusts(overrides):
    """Gusts of 5 m/s from both forecasts, except overrides: {UTC hour: (previous_day1, previous_day2)}."""
    return by_value(wind_gusts_10m=lambda n, hour: overrides.get(hour, (5.0, 5.0))[n - 1])


def one_day(tmp_path, day, value, margin=7):
    folder = write(tmp_path / "set_c", int(day[:4]), answer(f"{day[:7]}-01", f"{day[:7]}-28", value))
    days = pd.DatetimeIndex([day])
    hourly = read_hourly(folder, years_for(days), TODAY)
    return daily_forecasts(hourly, days, cutoff_utc(days, "18:00"), margin).iloc[0]


# Which forecast each hour takes


def test_the_first_hour_of_the_day_takes_the_newer_one_day_forecast(tmp_path):
    first = pd.Timestamp("2025-01-15 06:00")  # covers 05:00 to 06:00 UTC, the first hour after midnight EST
    assert one_day(tmp_path, WINTER, gusts({first: (40.0, 10.0)}))["gust_max_ms"] == 40


def test_the_last_hour_of_the_day_never_takes_a_forecast_made_after_the_cutoff(tmp_path):
    # The one-day forecast for 2025-01-16 05:00 UTC comes from the 00 UTC run of 2025-01-15, after the cutoff.
    last = pd.Timestamp("2025-01-16 05:00")
    day = one_day(tmp_path, WINTER, gusts({last: (50.0, 10.0)}))
    assert day["gust_max_ms"] == 10
    # The newest value used comes from the 12 UTC run of the 14th, counted as available 7 hours later.
    assert day["known_by_utc"] == pd.Timestamp("2025-01-14 19:00", tz="UTC")


@pytest.mark.parametrize("day, hour, expected", [
    (WINTER, "2025-01-15 17:00", 30),  # from the 12 UTC run of the 14th: available 19:00 UTC, cutoff 23:00 UTC
    (WINTER, "2025-01-15 18:00", 5),   # from the 18 UTC run: available 01:00 UTC on the 15th, after the cutoff
    (SUMMER, "2025-07-15 17:00", 30),  # with daylight saving the cutoff is 22:00 UTC, still after 19:00
    (SUMMER, "2025-07-15 18:00", 5),
])
def test_the_one_day_forecast_comes_only_from_runs_published_before_the_cutoff(tmp_path, day, hour, expected):
    assert one_day(tmp_path, day, gusts({pd.Timestamp(hour): (30.0, 1.0)}))["gust_max_ms"] == expected


def test_availability_follows_the_run_not_the_hour(tmp_path):
    # 06:00 to 11:00 UTC come from the 06 UTC run and 12:00 to 17:00 from the 12 UTC run of the day before.
    summer = one_day(tmp_path, SUMMER, gusts({}))
    assert summer["known_by_utc"] == pd.Timestamp("2025-07-14 19:00", tz="UTC")
    assert summer["known_by_utc"] <= pd.Timestamp("2025-07-14 22:00", tz="UTC")


def test_the_issue_margin_moves_the_boundary(tmp_path):
    # The 18 UTC run of the 14th counts in winter only with a margin of 5 hours or less (23:00 UTC cutoff).
    evening = {pd.Timestamp("2025-01-15 20:00"): (30.0, 1.0)}
    assert one_day(tmp_path, WINTER, gusts(evening), margin=7)["gust_max_ms"] == 5
    assert one_day(tmp_path, WINTER, gusts(evening), margin=5)["gust_max_ms"] == 30


def test_an_hour_on_the_day_boundary_belongs_to_the_day_it_ends(tmp_path):
    midnight = pd.Timestamp("2025-01-15 05:00")  # covers 04:00 to 05:00 UTC, the last hour of 14 January EST
    value = gusts({midnight: (99.0, 99.0)})
    assert one_day(tmp_path, WINTER, value)["gust_max_ms"] == 5
    assert one_day(tmp_path, "2025-01-14", value)["gust_max_ms"] == 99


def test_a_missing_one_day_value_falls_back_and_a_hole_stays_missing(tmp_path):
    early = pd.Timestamp("2025-01-15 07:00")
    assert one_day(tmp_path, WINTER, gusts({early: (None, 7.0)}))["gust_max_ms"] == 7
    hole = one_day(tmp_path, WINTER, gusts({early: (None, None)}))
    assert pd.isna(hole["gust_max_ms"]) and hole["precip_mm"] == 0  # only the gusts miss an hour


def test_daily_values_and_units(tmp_path):
    early, late = pd.Timestamp("2025-01-15 08:00"), pd.Timestamp("2025-01-16 02:00")

    def temperature(n, hour):
        return {(early, 1): 10.0, (late, 2): -5.0}.get((hour, n), 0.0)

    value = by_value(precipitation=lambda n, hour: float(n), snowfall=lambda n, hour: 0.1, temperature_2m=temperature)
    day = one_day(tmp_path, WINTER, value)
    assert day["precip_mm"] == 12 * 1.0 + 12 * 2.0  # in January 12 hours take each forecast
    assert day["snowfall_mm"] == 24.0  # 0.1 cm an hour is 1 mm, for 24 hours
    assert (day["tmax_c"], day["tmin_c"]) == (10.0, -5.0)


def test_a_day_past_the_stored_hours_is_missing(tmp_path):
    folder = write(tmp_path / "set_c", 2025, answer("2025-01-01", "2025-01-15"))
    days = pd.DatetimeIndex(["2025-01-15"])  # its last hours fall on 2025-01-16, not stored
    day = daily_forecasts(read_hourly(folder, [2025], TODAY), days, cutoff_utc(days, "18:00"), 7).iloc[0]
    assert day[list(VALUES)].isna().all()
    assert pd.isna(day["known_by_utc"])  # the hours it has are behind no value the row keeps


# Reading #8's files


def test_read_hourly_checks_units_time_zone_columns_times_and_numbers(tmp_path):
    good = answer("2025-01-01", "2025-01-02")
    without = {k: v for k, v in good["hourly"].items() if k != "precipitation_previous_day2"}
    cases = [
        ({**good, "hourly_units": {**good["hourly_units"], "snowfall_previous_day1": "mm"}}, "expected 'cm'"),
        ({**good, "utc_offset_seconds": -18000}, "not in UTC"),
        ({**good, "hourly": without}, "lacks"),
        (answer("2025-01-01", "2025-01-02", time_format="%Y-%m-%d %H:%M"), "times are not"),
        (answer("2025-01-01", "2025-01-02", value=lambda column, hour: "x"), "not numbers"),
    ]
    for body, message in cases:
        write(tmp_path, 2025, body)
        with pytest.raises(ValueError, match=message):
            read_hourly(tmp_path, [2025], TODAY)


def test_every_year_before_this_one_must_be_stored(tmp_path):
    write(tmp_path, 2025, answer("2025-01-01", "2025-01-02"))
    with pytest.raises(FileNotFoundError, match="2024.json"):
        read_hourly(tmp_path, [2024, 2025], TODAY)
    # 2023 is before set c starts and 2026 is this year, not downloaded yet: neither is an error.
    assert len(read_hourly(tmp_path, [2023, 2025, 2026], TODAY)) == 48


def test_a_missing_current_year_is_named_in_a_warning(tmp_path, caplog):
    write(tmp_path, 2025, answer("2025-01-01", "2025-01-02"))
    with caplog.at_level(logging.WARNING, logger="period_files"):
        assert len(read_hourly(tmp_path, [2025, 2026], TODAY)) == 48
    assert "2026.json is not stored yet" in caplog.text and "forecast weather values (wx_*" in caplog.text
    assert "run open_meteo.py --only day_before_ncep_gfs_global" in caplog.text


def test_an_hour_stored_in_two_files_is_refused(tmp_path):
    write(tmp_path, 2024, answer("2024-12-31", "2025-01-01"))
    write(tmp_path, 2025, answer("2025-01-01", "2025-01-02"))
    with pytest.raises(ValueError, match="two files"):
        read_hourly(tmp_path, [2024, 2025], TODAY)


def test_a_day_needs_the_file_of_its_last_hour():
    assert years_for(pd.DatetimeIndex(["2025-06-30", "2025-12-31"])) == [2025, 2026]
