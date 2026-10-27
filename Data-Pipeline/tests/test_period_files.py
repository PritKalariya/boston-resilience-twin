import logging

import pytest

from period_files import stored_periods


def stored(folder, *periods):
    folder.mkdir(parents=True, exist_ok=True)
    for period in periods:
        (folder / f"{period}.json").write_text("{}")
    return folder


def test_a_missing_past_period_stops_the_run(tmp_path):
    folder = stored(tmp_path, 2024)
    with pytest.raises(FileNotFoundError, match="lacks 2023.json, 2025.json; run the download"):
        stored_periods(folder, [2023, 2024, 2025, 2026], 2026, "run the download", "x")


def test_the_current_and_later_periods_may_be_missing_and_each_is_named(tmp_path, caplog):
    folder = stored(tmp_path, 2025)
    with caplog.at_level(logging.WARNING, logger="period_files"):
        assert stored_periods(folder, [2025, 2026, 2027], 2026, "run the download", "the wx values") == [2025]
    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.WARNING]
    assert "2026.json is not stored yet (current period 2026), so the wx values stay blank" in caplog.records[0].getMessage()
    assert "2027.json" in caplog.records[1].getMessage() and "run the download" in caplog.text


def test_half_years_compare_in_time_order(tmp_path, caplog):
    folder = stored(tmp_path, "2025-H2", "2026-H1")
    with caplog.at_level(logging.WARNING, logger="period_files"):
        assert stored_periods(folder, ["2025-H2", "2026-H1", "2026-H2"], "2026-H2", "run tides.py", "x") == ["2025-H2", "2026-H1"]
    assert "2026-H2.json is not stored yet" in caplog.text
    with pytest.raises(FileNotFoundError, match="lacks 2026-H1.json"):
        stored_periods(stored(tmp_path / "older", "2025-H2"), ["2025-H2", "2026-H1"], "2026-H2", "run tides.py", "x")


def test_nothing_is_logged_when_every_file_is_stored(tmp_path, caplog):
    folder = stored(tmp_path, 2025, 2026)
    with caplog.at_level(logging.WARNING, logger="period_files"):
        assert stored_periods(folder, [2025, 2026], 2026, "run the download", "x") == [2025, 2026]
    assert not caplog.records
