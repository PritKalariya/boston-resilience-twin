import sys

import pandas as pd
import pytest

import daily_counts
from daily_counts import COLUMNS, STORM_GROUPS, daily_counts as count

UNITS = ["East", "West"]


def located(rows):
    """rows: (open_dt, unit or None, storm_group or None, location, after_switch)."""
    df = pd.DataFrame(rows, columns=["open_dt", "unit", "storm_group", "location", "after_switch"])
    return df.assign(
        open_dt=pd.to_datetime(df["open_dt"]),
        source_system="legacy",
        category=df["storm_group"].map(lambda g: "Pothole" if g is None else f"{g} category"),
        after_switch=df["after_switch"].astype(bool),
    )


ROWS = [
    ("2024-01-01 08:00", "East", "snow_ice", "placed", False),
    ("2024-01-01 23:59", "East", "snow_ice", "placed", True),
    ("2024-01-02 09:00", "West", "tree", "placed", False),
    ("2024-01-02 09:00", "West", None, "placed", False),
    ("2024-01-02 10:00", None, "tree", "no_coordinates", False),
    ("2024-01-03 10:00", None, "flooding", "placeholder", False),
    ("2024-01-09 10:00", "East", "tree", "placed", False),
]


def run(rows=ROWS, policy="keep", start="2024-01-01", end="2024-01-03"):
    return count(located(rows), UNITS, policy, start, end)


def cell(counts, unit, date, group, column="requests"):
    hit = counts[(counts["unit"] == unit) & (counts["date"] == pd.Timestamp(date)) & (counts["storm_group"] == group)]
    assert len(hit) == 1
    return int(hit[column].iloc[0])


def test_every_unit_day_and_group_is_present():
    counts, _ = run()
    assert list(counts.columns) == COLUMNS
    assert len(counts) == len(UNITS) * 3 * len(STORM_GROUPS)
    assert not counts.duplicated(["unit", "date", "storm_group"]).any()
    assert cell(counts, "East", "2024-01-01", "snow_ice") == 2
    assert cell(counts, "West", "2024-01-02", "tree") == 1
    assert cell(counts, "West", "2024-01-03", "flooding") == 0


def test_steps_reconcile_every_row():
    counts, report = run()
    assert report["requests"].iloc[0] == len(ROWS)
    assert report["requests"].iloc[:-1].sum() == report["requests"].iloc[-1] == counts["requests"].sum() == 3
    steps = dict(zip(report["step"], report["requests"]))
    assert steps["not storm-related"] == -1
    assert steps["no location: no_coordinates"] == -1
    assert steps["no location: placeholder"] == -1
    assert steps["outside 2024-01-01 to 2024-01-03"] == -1


def test_policies():
    keep, _ = run(policy="keep")
    drop, report = run(policy="drop")
    exclude, _ = run(policy="exclude_from_baselines")
    assert cell(keep, "East", "2024-01-01", "snow_ice", "baseline_requests") == 2
    assert cell(drop, "East", "2024-01-01", "snow_ice") == 1
    assert dict(zip(report["step"], report["requests"]))["after_switch rows removed (drop)"] == -1
    assert cell(exclude, "East", "2024-01-01", "snow_ice") == 2
    assert cell(exclude, "East", "2024-01-01", "snow_ice", "baseline_requests") == 1


def test_empty_input_gives_an_all_zero_table():
    counts, report = run(rows=[])
    assert len(counts) == len(UNITS) * 3 * len(STORM_GROUPS)
    assert counts["requests"].sum() == 0
    assert report["requests"].iloc[-1] == 0


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        count(located(ROWS).drop(columns="location"), UNITS, "keep", "2024-01-01", "2024-01-03")


def test_malformed_input_is_rejected():
    with pytest.raises(ValueError, match="unknown storm groups"):
        run(rows=[("2024-01-01", "East", "hail", "placed", False)])
    with pytest.raises(ValueError, match="without a unit"):
        run(rows=[("2024-01-01", None, "tree", "placed", False)])
    with pytest.raises(ValueError, match="not in the neighborhood layer"):
        run(rows=[("2024-01-01", "Atlantis", "tree", "placed", False)])
    with pytest.raises(ValueError, match="unknown"):
        run(policy="sometimes")


def test_missing_open_date_is_counted_out():
    _, report = run(rows=[(None, "East", "tree", "placed", False)])
    assert dict(zip(report["step"], report["requests"]))["no open date"] == -1


def test_main_needs_an_end_when_there_is_nothing_to_count(tmp_path, monkeypatch):
    located(ROWS[:0]).to_parquet(tmp_path / "located.parquet", index=False)
    pd.DataFrame({"neighborhood": UNITS}).to_parquet(tmp_path / "hoods.parquet", index=False)
    (tmp_path / "processing.csv").write_text("setting,value\nafter_switch_policy,keep\n")
    argv = ["daily_counts.py", "--located", str(tmp_path / "located.parquet"), "--neighborhoods", str(tmp_path / "hoods.parquet"),
            "--config", str(tmp_path / "processing.csv"), "--out", str(tmp_path / "counts.parquet")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="pass --end"):
        daily_counts.main()
    monkeypatch.setattr(sys, "argv", argv + ["--start", "2024-01-01", "--end", "2024-01-02"])
    daily_counts.main()
    assert len(pd.read_parquet(tmp_path / "counts.parquet")) == len(UNITS) * 2 * len(STORM_GROUPS)


def test_midnight_starts_the_next_day_and_both_ends_of_the_range_count():
    # open_dt is Boston local time (ADR-020), so a request's day is its wall-clock date.
    rows = [
        ("2023-12-31 23:59:59", "East", "tree", "placed", False),  # before the start
        ("2024-01-01 00:00:00", "East", "tree", "placed", False),  # the start day's first second
        ("2024-01-01 23:59:59", "East", "tree", "placed", False),
        ("2024-01-02 00:00:00", "East", "tree", "placed", False),  # midnight belongs to the next day
        ("2024-01-03 23:59:59", "East", "tree", "placed", False),  # the end day's last second
        ("2024-01-04 00:00:00", "East", "tree", "placed", False),  # after the end
    ]
    counts, report = run(rows=rows)
    assert [cell(counts, "East", day, "tree") for day in ("2024-01-01", "2024-01-02", "2024-01-03")] == [2, 1, 1]
    assert dict(zip(report["step"], report["requests"]))["outside 2024-01-01 to 2024-01-03"] == -2


def test_daylight_saving_days_keep_their_own_requests():
    # Local times are counted as they are: the 23-hour and 25-hour days are ordinary days,
    # and no request moves to the day before or after.
    rows = [
        ("2024-03-10 00:30", "West", "snow_ice", "placed", False),
        ("2024-03-10 03:30", "West", "snow_ice", "placed", False),  # just after the clocks go forward
        ("2024-11-03 01:30", "West", "snow_ice", "placed", False),  # the hour that happens twice
        ("2024-11-03 23:30", "West", "snow_ice", "placed", False),
    ]
    counts, _ = run(rows=rows, start="2024-03-10", end="2024-11-03")
    assert cell(counts, "West", "2024-03-10", "snow_ice") == 2
    assert cell(counts, "West", "2024-11-03", "snow_ice") == 2
    assert counts["requests"].sum() == 4


def test_last_full_day_is_the_day_before_the_earlier_systems_last_request():
    df = pd.DataFrame({
        "source_system": ["legacy", "legacy", "new"],
        "open_dt": pd.to_datetime(["2026-07-09 12:00", "2026-07-10 00:00:01", "2026-10-11 23:00"], format="ISO8601"),
    })
    # The legacy system's last request is one second into July 10, so July 10 is a part day.
    assert daily_counts.last_full_day(df) == pd.Timestamp("2026-07-09")


def test_main_without_an_end_counts_up_to_the_last_full_day(tmp_path, monkeypatch, capsys):
    legacy = located([("2024-01-01 08:00", "East", "tree", "placed", False),
                      ("2024-01-03 00:30", "East", "tree", "placed", False)])
    new = located([("2024-01-05 12:00", "West", "tree", "placed", False)]).assign(source_system="new")
    pd.concat([legacy, new], ignore_index=True).to_parquet(tmp_path / "located.parquet", index=False)
    pd.DataFrame({"neighborhood": UNITS}).to_parquet(tmp_path / "hoods.parquet", index=False)
    (tmp_path / "processing.csv").write_text("setting,value\nafter_switch_policy,keep\n")
    monkeypatch.setattr(sys, "argv", [
        "daily_counts.py", "--located", str(tmp_path / "located.parquet"), "--neighborhoods", str(tmp_path / "hoods.parquet"),
        "--config", str(tmp_path / "processing.csv"), "--start", "2024-01-01", "--out", str(tmp_path / "counts.parquet")])
    daily_counts.main()
    counts = pd.read_parquet(tmp_path / "counts.parquet")
    # The legacy system stops at 2024-01-03 00:30, so 2024-01-02 is the last day both systems cover.
    assert counts["date"].max() == pd.Timestamp("2024-01-02")
    assert counts["requests"].sum() == 1
    assert "(2 units x 2 days x 3 groups)" in capsys.readouterr().out
