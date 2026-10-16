import pandas as pd
import pytest

from legacy_311 import count_records, parse_timestamps, read_legacy, read_years

HEADER = "case_enquiry_id,open_dt,closed_dt,type,neighborhood,latitude,longitude\n"


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    return path


def test_empty_file(tmp_path):
    df, report = read_legacy(write(tmp_path, "2015.csv", ""), year=2015)
    assert df.empty
    assert report.file_rows == 0 and report.dropped_rows == 0


def test_header_only(tmp_path):
    df, report = read_legacy(write(tmp_path, "2015.csv", HEADER), year=2015)
    assert df.empty
    assert report.parsed_rows == 0


def test_missing_required_column(tmp_path):
    path = write(tmp_path, "2015.csv", "case_enquiry_id,type\n1,Pothole\n")
    with pytest.raises(ValueError, match="open_dt"):
        read_legacy(path)


def test_mixed_timestamp_formats_all_parse():
    values = pd.Series([
        "2015-02-08 07:19:55",
        "2015-02-08 07:19:55.7",
        "2015-02-08 07:19:55.78",
        "2015-02-08 07:19:55.783",
    ])
    parsed = parse_timestamps(values)
    assert parsed.notna().all()
    assert parsed.iloc[3].microsecond == 783000


def test_mixed_formats_in_file_keep_every_row(tmp_path):
    body = HEADER + (
        "1,2015-01-01 08:00:00.12,,Pothole,Dorchester,42.3,-71.0\n"
        "2,2015-01-01 09:00:00,,Pothole,Roxbury,42.3,-71.0\n"
        "3,2015-01-01 10:00:00.5,,Pothole,Roxbury,42.3,-71.0\n"
    )
    df, report = read_legacy(write(tmp_path, "2015.csv", body), year=2015)
    assert len(df) == 3
    assert report.dropped_rows == 0


def test_bad_rows_are_counted_by_reason(tmp_path):
    body = HEADER + (
        "1,2015-01-01 08:00:00,,Pothole,Dorchester,42.3,-71.0\n"
        "1,2015-01-01 08:00:00,,Pothole,Dorchester,42.3,-71.0\n"
        ",2015-01-01 08:00:00,,Pothole,Dorchester,42.3,-71.0\n"
        "4,not a date,,Pothole,Dorchester,42.3,-71.0\n"
        "5,2014-12-31 23:59:59,,Pothole,Dorchester,42.3,-71.0\n"
        "6,2015-01-02 08:00:00,,Pothole,Dorchester,oops,-71.0\n"
    )
    df, report = read_legacy(write(tmp_path, "2015.csv", body), year=2015)
    assert report.dropped == {
        "duplicate_case_id": 1,
        "missing_case_id": 1,
        "bad_open_dt": 1,
        "open_dt_outside_file_year": 1,
    }
    assert report.parsed_rows == len(df) == 2
    assert df["latitude"].isna().sum() == 1


def test_malformed_line_is_counted(tmp_path):
    body = HEADER + (
        "1,2015-01-01 08:00:00,,Pothole,Dorchester,42.3,-71.0\n"
        "2,2015-01-01 08:00:00,,Pothole,Dorchester,42.3,-71.0,extra,fields\n"
        "3,2015-01-01 08:00:00,,Pothole\n"
        "4,2015-01-01 09:00:00,,Pothole,Dorchester,42.3,-71.0\n"
    )
    df, report = read_legacy(write(tmp_path, "2015.csv", body), year=2015)
    assert df["case_enquiry_id"].tolist() == ["1", "4"]
    assert report.dropped == {"malformed_line": 2}


def test_quoted_newlines_count_as_one_record(tmp_path):
    body = HEADER + '1,2015-01-01 08:00:00,,"Pot\nhole",Dorchester,42.3,-71.0\n'
    assert count_records(write(tmp_path, "2015.csv", body)) == 1


def test_duplicates_across_years_are_dropped(tmp_path):
    row = "1,{dt},,Pothole,Dorchester,42.3,-71.0\n"
    write(tmp_path, "2015.csv", HEADER + row.format(dt="2015-12-31 23:00:00"))
    write(tmp_path, "2016.csv", HEADER + row.format(dt="2016-01-01 01:00:00"))
    df, reports = read_years(tmp_path, [2015, 2016])
    assert len(df) == 1
    assert reports[-1].dropped == {"duplicate_case_id_across_years": 1}
