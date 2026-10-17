import pandas as pd
import pytest

from new_311 import read_new_system, to_boston

HEADER = "case_id,open_date,close_date,service_name,neighborhood,latitude,longitude\n"


def write(tmp_path, body):
    path = tmp_path / "new_system.csv"
    path.write_text(body)
    return path


def test_empty_file(tmp_path):
    df, report = read_new_system(write(tmp_path, ""))
    assert df.empty
    assert report.file_rows == 0


def test_header_only(tmp_path):
    df, report = read_new_system(write(tmp_path, HEADER))
    assert df.empty
    assert report.parsed_rows == 0


def test_missing_required_column(tmp_path):
    with pytest.raises(ValueError, match="service_name"):
        read_new_system(write(tmp_path, "case_id,open_date\nBCS-1,2026-07-14 10:56:30+00\n"))


def test_labelled_utc_is_kept_as_boston_wall_time():
    out = to_boston(pd.Series(["2026-07-14 10:56:30.994145+00", "2026-01-14 10:56:30+00"]))
    assert out.tolist() == [pd.Timestamp("2026-07-14 10:56:30.994145"), pd.Timestamp("2026-01-14 10:56:30")]


def test_true_utc_is_converted_across_dst():
    out = to_boston(pd.Series(["2026-01-15 15:00:00+00", "2026-07-15 15:00:00+00"]), clock="utc")
    assert out.tolist() == [pd.Timestamp("2026-01-15 10:00"), pd.Timestamp("2026-07-15 11:00")]


def test_unknown_clock():
    with pytest.raises(ValueError, match="clock"):
        to_boston(pd.Series(["2026-01-15 15:00:00+00"]), clock="gmt")


def test_bad_rows_are_counted_by_reason(tmp_path):
    body = HEADER + (
        "BCS-1,2026-07-14 10:00:00+00,,Pothole,Dorchester,42.3,-71.0\n"
        "BCS-1,2026-07-14 10:00:00+00,,Pothole,Dorchester,42.3,-71.0\n"
        ",2026-07-14 10:00:00+00,,Pothole,Dorchester,42.3,-71.0\n"
        "BCS-4,yesterday,,Pothole,Dorchester,42.3,-71.0\n"
        "BCS-5,2026-07-14 10:00:00-04,,Pothole,Dorchester,42.3,-71.0\n"
        "BCS-6,2026-07-14 11:00:00+00,,Pothole,Dorchester,north,-71.0\n"
    )
    df, report = read_new_system(write(tmp_path, body))
    assert report.dropped == {
        "duplicate_case_id": 1,
        "missing_case_id": 1,
        "bad_open_date": 1,
        "unexpected_offset": 1,
    }
    assert df["case_id"].tolist() == ["BCS-1", "BCS-6"]
    assert df["latitude"].isna().sum() == 1


def test_malformed_line_is_counted(tmp_path):
    body = HEADER + (
        "BCS-1,2026-07-14 10:00:00+00,,Pothole,Dorchester,42.3,-71.0\n"
        "BCS-2,2026-07-14 10:00:00+00,,Pothole,Dorchester,42.3,-71.0,extra\n"
    )
    df, report = read_new_system(write(tmp_path, body))
    assert df["case_id"].tolist() == ["BCS-1"]
    assert report.dropped == {"malformed_line": 1}
