"""forecast_parity.py (#160) on an evening answer and an archive that open_meteo.py stores from fake answers."""

import json
from datetime import date, datetime, timezone

import pandas as pd

import forecast_parity
import open_meteo
import open_meteo_forecast as om
from surge_labels import load_settings
from test_feature_sources import CONFIG, PreviousRuns

SETTINGS = load_settings(CONFIG / "labels_features.csv")
EVENING = datetime(2024, 2, 2, 23, 30, tzinfo=timezone.utc)  # 18:30 in Boston; D = 2024-02-03
LATER = datetime(2024, 2, 5, 12, tzinfo=timezone.utc)
# With the 7-hour margin, D's previous_day1 values are known at the cutoff for hours before 18:00 UTC
# (runs up to 12 UTC on D-1); later hours use previous_day2.
FIRST_UNKNOWN = "2024-02-03T18:00"


def store(root, now, value, evening):
    sets = open_meteo.read_config(CONFIG / "open_meteo.csv", today=now.date())
    run = open_meteo.run_download(sets, root / "raw", PreviousRuns(value), state_root=root / "state", now=now,
                                  only=[om.SET], evening=evening)
    assert not run["failed"], run


def run(root, now=LATER):
    state = root / "state" / om.SET
    return forecast_parity.parity(state / "evening", root / "raw" / om.SET, state, SETTINGS, pd.Timestamp(now))


def filled_late(name, hour):
    """What the API answers in the evening: previous_day1 of the hours whose day-before run has not started is
    filled from the newest run (here 9.0)."""
    return 9.0 if name.endswith("_previous_day1") and hour >= FIRST_UNKNOWN else 1.0


def test_hours_the_rule_does_not_use_differ_and_the_daily_values_do_not(tmp_path):
    store(tmp_path, EVENING, filled_late, evening=True)
    store(tmp_path, LATER, lambda name, hour: 1.0, evening=False)
    table, skipped = run(tmp_path)
    assert skipped == [] and list(table["as_of"]) == ["2024-02-02"]
    row = table.iloc[0]
    assert row["day"] == date(2024, 2, 3) and row["worst"] == 0.0
    # 12 station hours from 18:00 to 05:00 UTC, in each of the 4 values the features read.
    assert (row["raw_day1_differ"], row["raw_day2_differ"]) == (12 * 4, 0)


def test_a_value_the_rule_uses_that_changes_shows(tmp_path):
    store(tmp_path, EVENING, lambda name, hour: 1.0, evening=True)
    store(tmp_path, LATER, lambda name, hour: 3.0 if name == "precipitation_previous_day1" and hour == "2024-02-03T10:00"
          else 1.0, evening=False)
    row = run(tmp_path)[0].iloc[0]
    assert row["precip_mm"] == 2.0 and row["worst"] > forecast_parity.TOLERANCE


def test_an_answer_from_before_the_cutoff_or_an_archive_not_final_yet_is_left_out(tmp_path):
    store(tmp_path, EVENING, lambda name, hour: 1.0, evening=True)
    table, skipped = run(tmp_path, now=EVENING)  # the year file was last fetched in the evening itself
    assert table.empty and skipped == [
        "2024-02-02: the year files were not fetched after 2024-02-04 12:00:00+00:00 yet (rerun open_meteo.py)"]
    captures = tmp_path / "state" / om.SET / "evening" / "_captures.jsonl"
    line = json.loads(captures.read_text())
    captures.write_text(json.dumps({**line, "fetched_at": "2024-02-02T21:00:00+00:00"}) + "\n")  # 16:00 in Boston
    store(tmp_path, LATER, lambda name, hour: 1.0, evening=False)
    table, skipped = run(tmp_path)
    assert table.empty and skipped == ["2024-02-02: fetched 2024-02-02 21:00:00+00:00, before the cutoff "
                                       "2024-02-02 23:00:00+00:00"]
