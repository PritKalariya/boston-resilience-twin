import json
from datetime import date

import pandas as pd
import pytest

from tide_features import read_predictions, tide_features

TODAY = date(2026, 10, 26)


def answer(start, end, value=lambda hour: 1.0, freq="h"):
    """CO-OPS predictions as #7 stores them: GMT times and metres as text."""
    hours = pd.date_range(f"{start} 00:00", f"{end} 23:00", freq=freq)
    return {"predictions": [{"t": f"{hour:%Y-%m-%d %H:%M}", "v": "" if value(hour) is None else f"{value(hour):.3f}"}
                            for hour in hours]}


def write(folder, name, body):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(body))
    return folder


def year_of(tmp_path, year, value=lambda hour: 1.0):
    folder = tmp_path / "predicted"
    write(folder, f"{year}-H1", answer(f"{year}-01-01", f"{year}-06-30", value))
    return write(folder, f"{year}-H2", answer(f"{year}-07-01", f"{year}-12-31", value))


def days_of(table):
    return table.set_index("date")


def test_a_boston_day_runs_from_local_midnight_to_local_midnight(tmp_path):
    peaks = {pd.Timestamp("2025-01-15 04:00"): 3.0,   # 23:00 EST on 14 January
             pd.Timestamp("2025-01-15 05:00"): 2.5}   # 00:00 EST on 15 January
    folder = year_of(tmp_path, 2025, lambda hour: peaks.get(hour, 1.0))
    table = days_of(tide_features(pd.DatetimeIndex(["2025-01-14", "2025-01-15", "2025-01-16"]), folder, TODAY))
    assert list(table["tide_pred_max_m"]) == [3.0, 2.5, 1.0]
    assert list(table["tide_pred_hours"]) == [24, 24, 24]


def test_daylight_saving_days_have_23_and_25_hours(tmp_path):
    last_hour = pd.Timestamp("2025-11-03 04:00")  # 23:00 EST on 2 November, the day's 25th hour
    folder = year_of(tmp_path, 2025, lambda hour: 4.0 if hour == last_hour else 1.0)
    table = days_of(tide_features(pd.DatetimeIndex(["2025-03-09", "2025-11-02"]), folder, TODAY))
    assert list(table["tide_pred_hours"]) == [23, 25]
    assert list(table["tide_pred_max_m"]) == [1.0, 4.0]


def test_a_day_without_every_hour_gets_no_maximum(tmp_path):
    gap = pd.Timestamp("2025-01-15 12:00")
    folder = year_of(tmp_path, 2025, lambda hour: None if hour == gap else 1.0)
    table = days_of(tide_features(pd.DatetimeIndex(["2025-01-15", "2025-01-16"]), folder, TODAY))
    assert table.loc["2025-01-15", "tide_pred_hours"] == 23 and pd.isna(table.loc["2025-01-15", "tide_pred_max_m"])
    assert table.loc["2025-01-16", "tide_pred_max_m"] == 1.0


def test_new_years_eve_takes_its_last_hours_from_the_next_years_file(tmp_path):
    year_of(tmp_path, 2025)
    folder = write(tmp_path / "predicted", "2026-H1", answer("2026-01-01", "2026-06-30", lambda hour: 2.0))
    eve = tide_features(pd.DatetimeIndex(["2025-12-31"]), folder, TODAY).iloc[0]
    assert (eve["tide_pred_hours"], eve["tide_pred_max_m"]) == (24, 2.0)  # 00:00 to 04:00 UTC on 1 January


def test_every_past_half_year_must_be_stored(tmp_path):
    folder = write(tmp_path / "predicted", "2025-H2", answer("2025-07-01", "2025-12-31"))
    with pytest.raises(FileNotFoundError, match="2025-H1.json"):
        read_predictions(folder, pd.DatetimeIndex(["2025-03-01"]), TODAY)
    # 2026-H2 is the current half-year, not downloaded yet: its days stay missing.
    day = tide_features(pd.DatetimeIndex(["2026-10-20"]), folder, TODAY).iloc[0]
    assert day["tide_pred_hours"] == 0 and pd.isna(day["tide_pred_max_m"])


def test_predictions_must_be_hourly_numbers(tmp_path):
    write(tmp_path, "2025-H1", answer("2025-01-01", "2025-01-02", freq="30min"))
    with pytest.raises(ValueError, match="not hourly"):
        read_predictions(tmp_path, pd.DatetimeIndex(["2025-01-01"]), TODAY)
    bad = answer("2025-01-01", "2025-01-02")
    bad["predictions"][3]["v"] = "high"
    write(tmp_path, "2025-H1", bad)
    with pytest.raises(ValueError, match="not a number"):
        read_predictions(tmp_path, pd.DatetimeIndex(["2025-01-01"]), TODAY)
