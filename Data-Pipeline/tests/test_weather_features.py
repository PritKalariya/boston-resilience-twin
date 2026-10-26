import json
from datetime import date

import pandas as pd
import pytest

from open_meteo_forecast import DAYS_BEFORE, TIME_FORMAT, VALUES, columns
from weather_features import (
    OBSERVED,
    STATION,
    WEATHER_FEATURES,
    read_observed,
    weather_columns,
    weather_features,
)

TODAY = date(2026, 10, 26)
DATATYPES = list(OBSERVED)  # PRCP, SNOW, TMAX, TMIN, AWND, WSF5


def observed(datatype, day):
    """A value that names its datatype and day, for example PRCP on 31 December is 1231 and SNOW 11231."""
    day = pd.Timestamp(day)
    return float(DATATYPES.index(datatype) * 10000 + day.month * 100 + day.day)


def noaa_answer(start, end, value=observed, attributes=lambda datatype, day: ",,W,2400", station=STATION):
    """A CDO answer as #7 stores it, with SNWD and WSF2 rows that the features must ignore."""
    days = pd.date_range(start, end)
    return {"metadata": {"resultset": {"offset": 1, "count": len(days) * 8, "limit": 1000}},
            "results": [{"date": f"{day:%Y-%m-%d}T00:00:00", "datatype": datatype, "station": station,
                         "attributes": attributes(datatype, day), "value": value(datatype, day) if datatype in OBSERVED else 0}
                        for day in days for datatype in [*DATATYPES, "SNWD", "WSF2"]]}


def forecast_answer(start, end):
    """#8's set c: precipitation 0.5 mm, snowfall 0, temperature 3 C and gusts 9 m/s in every hour."""
    hours = pd.date_range(f"{start} 00:00", f"{end} 23:00", freq="h")
    each = {"precipitation": 0.5, "snowfall": 0.0, "temperature_2m": 3.0, "wind_gusts_10m": 9.0}
    return {"utc_offset_seconds": 0,
            "hourly_units": {f"{name}_previous_day{n}": unit for name, unit, *_ in VALUES.values() for n in DAYS_BEFORE},
            "hourly": {"time": [f"{hour:{TIME_FORMAT}}" for hour in hours],
                       **{column: [each[column.rsplit("_previous_day", 1)[0]]] * len(hours) for column in columns()}}}


def write(folder, name, body):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(body))
    return folder


def sources(tmp_path):
    """Logan 2023 and 2024 (#7) and set c from 2024-01-21, where #8's starts."""
    noaa = tmp_path / "noaa_logan"
    for year in (2023, 2024):
        write(noaa, year, noaa_answer(f"{year}-01-01", f"{year}-12-31"))
    forecast = write(tmp_path / "set_c", 2024, forecast_answer("2024-01-21", "2024-02-29"))
    return noaa, forecast


def features(noaa, forecast, days, lag=2):
    return weather_features(pd.DatetimeIndex(days), "18:00", noaa, forecast, lag, 7, TODAY).set_index("date")


# ADR-003: observed in training, forecasts after


def test_training_rows_take_the_observed_day_and_later_rows_the_forecast(tmp_path):
    table = features(*sources(tmp_path), ["2023-12-31", "2024-01-01", "2024-01-22"])
    dec31, jan1, jan22 = (table.loc[pd.Timestamp(day)] for day in ("2023-12-31", "2024-01-01", "2024-01-22"))
    assert dec31["wx_source"] == "observed" and pd.isna(dec31["wx_known_by_utc"])
    assert (dec31["wx_precip_mm"], dec31["wx_snowfall_mm"], dec31["wx_gust_max_ms"]) == (
        observed("PRCP", "2023-12-31"), observed("SNOW", "2023-12-31"), observed("WSF5", "2023-12-31"))
    # Before set c starts: missing, and never the observation that exists for that day.
    assert jan1["wx_source"] == "forecast" and jan1[WEATHER_FEATURES].isna().all()
    assert jan22["wx_source"] == "forecast"
    assert (jan22["wx_precip_mm"], jan22["wx_tmax_c"], jan22["wx_tmin_c"], jan22["wx_gust_max_ms"]) == (12.0, 3.0, 3.0, 9.0)
    assert jan22["wx_known_by_utc"] <= pd.Timestamp("2024-01-21 23:00", tz="UTC")  # 18:00 EST on 21 January


def test_a_row_from_2024_on_never_reads_the_observations_of_its_day_or_the_day_before(tmp_path):
    noaa, forecast = sources(tmp_path)
    before = features(noaa, forecast, ["2024-01-22"])

    def changed(datatype, day):
        return 999.0 if day in (pd.Timestamp("2024-01-21"), pd.Timestamp("2024-01-22")) else observed(datatype, day)

    write(noaa, 2024, noaa_answer("2024-01-01", "2024-12-31", value=changed))
    pd.testing.assert_frame_equal(before, features(noaa, forecast, ["2024-01-22"]))


def test_lagged_observations_come_from_day_d_minus_the_lag(tmp_path):
    noaa, forecast = sources(tmp_path)
    lag2 = features(noaa, forecast, ["2024-01-22"]).iloc[0]
    assert (lag2["obs_prcp_mm_lag2"], lag2["obs_wsf5_ms_lag2"]) == (observed("PRCP", "2024-01-20"), observed("WSF5", "2024-01-20"))
    lag6 = features(noaa, forecast, ["2024-01-22"], lag=6).iloc[0]
    assert lag6["obs_awnd_ms_lag6"] == observed("AWND", "2024-01-16")
    with pytest.raises(ValueError, match="2 or more"):
        features(noaa, forecast, ["2024-01-22"], lag=1)


def test_columns_and_one_row_per_day(tmp_path):
    days = pd.date_range("2023-12-25", "2024-01-25")
    table = weather_features(days, "18:00", *sources(tmp_path), 2, 7, TODAY)
    assert list(table.columns) == ["date", *weather_columns(2)]
    assert len(table) == len(days) and not table["date"].duplicated().any()


def test_the_first_days_of_2015_lack_only_the_lag(tmp_path):
    noaa = write(tmp_path / "noaa_logan", 2015, noaa_answer("2015-01-01", "2015-12-31"))
    jan1 = weather_features(pd.DatetimeIndex(["2015-01-01"]), "18:00", noaa, None, 2, 7, TODAY).iloc[0]
    assert jan1["wx_precip_mm"] == observed("PRCP", "2015-01-01") and pd.isna(jan1["obs_prcp_mm_lag2"])  # 2014 is not stored


# Reading #7's files


def test_a_quality_flag_makes_a_value_missing_and_other_datatypes_are_ignored(tmp_path):
    def flags(datatype, day):
        if (datatype, day) == ("TMAX", pd.Timestamp("2023-06-01")):
            return ",X,W,2400"  # failed GHCN-D's extreme value check
        if (datatype, day) == ("PRCP", pd.Timestamp("2023-06-01")):
            return "T,,W,2400"  # a trace: a measurement flag, not a failed check
        return ",,W,2400"

    noaa = write(tmp_path / "noaa_logan", 2023, noaa_answer("2023-01-01", "2023-12-31", attributes=flags))
    values = read_observed(noaa, [2023], TODAY)
    assert list(values.columns) == list(OBSERVED.values())
    assert pd.isna(values.loc["2023-06-01", "tmax_c"]) and values.loc["2023-06-02", "tmax_c"] == observed("TMAX", "2023-06-02")
    assert values.loc["2023-06-01", "prcp_mm"] == observed("PRCP", "2023-06-01")


def test_another_station_or_a_repeated_day_is_refused(tmp_path):
    write(tmp_path, 2023, noaa_answer("2023-01-01", "2023-01-31", station="GHCND:USW00094701"))
    with pytest.raises(ValueError, match="stations"):
        read_observed(tmp_path, [2023], TODAY)
    twice = noaa_answer("2023-01-01", "2023-01-31")
    twice["results"] += twice["results"][:1]
    write(tmp_path, 2023, twice)
    with pytest.raises(ValueError, match="repeats"):
        read_observed(tmp_path, [2023], TODAY)


def test_every_past_year_must_be_stored(tmp_path):
    noaa = write(tmp_path / "noaa_logan", 2024, noaa_answer("2024-01-01", "2024-12-31"))
    with pytest.raises(FileNotFoundError, match="2023.json"):
        read_observed(noaa, [2023, 2024], TODAY)
    assert len(read_observed(noaa, [2024, 2026], TODAY)) == 366  # 2026 is this year, not downloaded yet


def test_rows_after_2023_need_the_forecasts(tmp_path):
    noaa, _ = sources(tmp_path)
    with pytest.raises(ValueError, match="forecast"):
        weather_features(pd.DatetimeIndex(["2024-02-01"]), "18:00", noaa, None, 2, 7, TODAY)
