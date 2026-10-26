import json
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

import legacy_311
import map_311
import new_311
from validate_data import ValidationFailed, Validator, load_bounds, main, validate_processed, validate_raw

BOUNDS_FILE = Path(__file__).resolve().parents[1] / "config" / "validation_bounds.csv"
BOUNDS = load_bounds(BOUNDS_FILE)
UNITS = ["Dorchester", "Roxbury"]
N = 200


def open_times(n=N, shift_hours=0):
    """Boston local times spread from 09:00 to 17:00 (median 13:00), across January 2024."""
    base = pd.Timestamp("2024-01-01 09:00")
    return [base + pd.Timedelta(days=i % 28, minutes=(i * 480 // n) + 60 * shift_hours) for i in range(n)]


def legacy_csv(path, n=N, shift_hours=0, drop=(), bad_dates=0):
    times = [t.strftime("%Y-%m-%d %H:%M:%S") for t in open_times(n, shift_hours)]
    times[:bad_dates] = ["not a date"] * bad_dates
    df = pd.DataFrame({c: "x" for c in legacy_311.KEEP_COLUMNS}, index=range(n))
    df["case_enquiry_id"] = [str(101000000 + i) for i in range(n)]
    df["open_dt"] = times
    df["latitude"], df["longitude"] = "42.31", "-71.06"
    df.drop(columns=list(drop)).to_csv(path, index=False)
    return path


def new_csv(path, n=N, shift_hours=0, suffix="+00", drop=(), blank_dates=0):
    df = pd.DataFrame({c: "x" for c in new_311.KEEP_COLUMNS}, index=range(n))
    df["case_id"] = [f"BCS-{i:08d}" for i in range(n)]
    df["open_date"] = [t.strftime("%Y-%m-%d %H:%M:%S") + suffix for t in open_times(n, shift_hours)]
    df.loc[: blank_dates - 1, "open_date"] = ""
    df["latitude"], df["longitude"] = "42.31", "-71.06"
    df.drop(columns=list(drop)).to_csv(path, index=False)
    return path


@pytest.fixture
def raw(tmp_path):
    raw_dir = tmp_path / "311"
    raw_dir.mkdir()
    legacy_csv(raw_dir / "2024.csv")
    new_csv(raw_dir / "new_system.csv")
    published = pd.DataFrame({"source": ["2024.csv", "new_system.csv"], "published_rows": [N, N]})
    return raw_dir, published


def run_raw(raw_dir, published, years=(2024,)):
    v = Validator()
    validate_raw(v, raw_dir, list(years), published, BOUNDS)
    return v


def failed(v):
    return {(c.table, c.expectation, c.column) for c in v.failed}


def warned(v):
    return {(c.table, c.expectation, c.column) for c in v.warnings}


# Raw stage

def test_clean_raw_files_pass(raw):
    v = run_raw(*raw)
    assert v.checks and not v.failed


def test_utc_shift_in_the_new_system_fails(raw):
    raw_dir, published = raw
    new_csv(raw_dir / "new_system.csv", shift_hours=5)
    assert failed(run_raw(raw_dir, published)) == {("raw new_system.csv", "expect_column_median_to_be_between", "open_hour")}


def test_utc_shift_in_a_legacy_file_fails(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", shift_hours=4)
    assert failed(run_raw(raw_dir, published)) == {("raw 2024.csv", "expect_column_median_to_be_between", "open_hour")}


def test_missing_column_fails_by_name(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", drop=["type"])
    new_csv(raw_dir / "new_system.csv", drop=["service_name"])
    assert failed(run_raw(raw_dir, published)) == {
        ("raw 2024.csv header", "expect_column_to_exist", "type"),
        ("raw new_system.csv header", "expect_column_to_exist", "service_name"),
    }


def test_missing_time_column_fails_without_crashing(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", drop=["open_dt"])
    names = failed(run_raw(raw_dir, published))
    assert ("raw 2024.csv header", "expect_column_to_exist", "open_dt") in names
    assert ("raw 2024.csv", "expect_column_median_to_be_between", "open_hour") in names


def test_a_different_offset_fails(raw):
    raw_dir, published = raw
    new_csv(raw_dir / "new_system.csv", suffix="-04")
    # Read with its offset, the time also moves 4 hours later, so the median check fails too.
    assert failed(run_raw(raw_dir, published)) == {
        ("raw new_system.csv", "expect_column_values_to_be_in_set", "offset_ok"),
        ("raw new_system.csv", "expect_column_median_to_be_between", "open_hour"),
    }


def test_optional_column_missing_only_warns(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", drop=["ward"])
    new_csv(raw_dir / "new_system.csv", drop=["precinct"])
    v = run_raw(raw_dir, published)
    assert not v.failed
    assert warned(v) == {
        ("raw 2024.csv header", "expect_column_to_exist", "ward"),
        ("raw new_system.csv header", "expect_column_to_exist", "precinct"),
    }


def test_missing_coordinates_column_fails(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", drop=["latitude"])
    assert ("raw 2024.csv header", "expect_column_to_exist", "latitude") in failed(run_raw(raw_dir, published))


def test_one_blank_new_system_time_passes(raw):
    # Kirtan's case on #83: 1 blank in 1,000 rows. The parser drops it; the stage must not fail.
    raw_dir, published = raw
    new_csv(raw_dir / "new_system.csv", n=1000, blank_dates=1)
    published.loc[published["source"] == "new_system.csv", "published_rows"] = 1000
    assert not run_raw(raw_dir, published).failed


def test_a_few_duplicate_ids_warn_and_many_fail(raw):
    raw_dir, published = raw
    path = legacy_csv(raw_dir / "2024.csv", n=2000)
    published.loc[published["source"] == "2024.csv", "published_rows"] = 2000
    df = pd.read_csv(path, dtype=str)
    df.loc[1, "case_enquiry_id"] = df.loc[0, "case_enquiry_id"]  # 1 of 2,000
    df.to_csv(path, index=False)
    v = run_raw(raw_dir, published)
    assert not v.failed
    assert warned(v) == {("raw 2024.csv", "expect_column_values_to_be_unique", "case_enquiry_id")}

    df.loc[1:40, "case_enquiry_id"] = df.loc[0, "case_enquiry_id"]  # 2% repeated
    df.to_csv(path, index=False)
    assert ("raw 2024.csv", "expect_column_values_to_be_unique", "case_enquiry_id") in failed(run_raw(raw_dir, published))


def test_truncated_new_system_export_fails(raw):
    raw_dir, published = raw
    new_csv(raw_dir / "new_system.csv", n=150)
    assert failed(run_raw(raw_dir, published)) == {("raw new_system.csv", "expect_table_row_count_to_be_between", "")}


def test_unparseable_dates_beyond_the_bound_fail(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", bad_dates=2)  # 1% of 200, above the 0.1% allowed
    assert ("raw 2024.csv", "expect_column_values_to_be_in_set", "open_parsed") in failed(run_raw(raw_dir, published))


def test_truncated_file_fails_the_published_count(raw):
    raw_dir, published = raw
    legacy_csv(raw_dir / "2024.csv", n=150)
    assert failed(run_raw(raw_dir, published)) == {("raw 2024.csv", "expect_table_row_count_to_be_between", "")}


def test_coordinates_outside_boston_fail(raw):
    raw_dir, published = raw
    path = legacy_csv(raw_dir / "2024.csv")
    df = pd.read_csv(path, dtype=str)
    df["latitude"], df["longitude"] = df["longitude"], df["latitude"]  # a whole file swapped
    df.to_csv(path, index=False)
    assert failed(run_raw(raw_dir, published)) == {("raw 2024.csv", "expect_column_values_to_be_in_set", "in_boston")}


def test_missing_and_empty_files_fail(raw):
    raw_dir, published = raw
    (raw_dir / "new_system.csv").write_text("")
    names = failed(run_raw(raw_dir, published, years=(2023, 2024)))
    assert ("raw 2023.csv", "file_exists_and_is_not_empty", "") in names
    assert ("raw new_system.csv", "file_exists_and_is_not_empty", "") in names


# Processed stage

def combined_frame(n=N, shift_hours=0):
    df = pd.DataFrame({c: pd.Series([None] * n, dtype="object") for c in map_311.COLUMNS})
    df["case_id"] = [f"C{i}" for i in range(n)]
    df["source_system"] = ["legacy" if i % 2 else "new" for i in range(n)]
    df["open_dt"] = open_times(n, shift_hours)
    df["storm_group"] = ["tree" if i % 3 == 0 else None for i in range(n)]
    df["match"] = "same"
    return df


def located_frame(combined, placed=1.0):
    df = combined.copy()
    n_placed = int(len(df) * placed)
    df["location"] = ["placed"] * n_placed + ["no_coordinates"] * (len(df) - n_placed)
    df["unit"] = [UNITS[i % 2] if i < n_placed else None for i in range(len(df))]
    df["geoid20"] = ["25025000100" if i < n_placed else None for i in range(len(df))]
    return df


def write_processed(tmp_path, combined, located):
    combined.to_parquet(tmp_path / "311_combined.parquet", index=False)
    located.to_parquet(tmp_path / "311_located.parquet", index=False)
    return tmp_path / "311_combined.parquet", tmp_path / "311_located.parquet"


def run_processed(paths):
    v = Validator()
    validate_processed(v, *paths, UNITS, date(2026, 10, 25), BOUNDS)
    return v


def test_clean_processed_tables_pass(tmp_path):
    combined = combined_frame()
    v = run_processed(write_processed(tmp_path, combined, located_frame(combined)))
    assert v.checks and not v.failed


def test_utc_shift_in_the_combined_table_fails(tmp_path):
    combined = combined_frame(shift_hours=5)
    names = failed(run_processed(write_processed(tmp_path, combined, located_frame(combined))))
    assert names == {
        ("311_combined legacy", "expect_column_median_to_be_between", "open_hour"),
        ("311_combined new", "expect_column_median_to_be_between", "open_hour"),
    }


def test_missing_column_in_the_combined_table_fails(tmp_path):
    combined = combined_frame()
    paths = write_processed(tmp_path, combined.drop(columns=["storm_group"]), located_frame(combined))
    names = failed(run_processed(paths))
    assert ("311_combined header", "expect_column_to_exist", "storm_group") in names


def test_unknown_unit_and_storm_group_fail(tmp_path):
    combined = combined_frame()
    combined.loc[0, "storm_group"] = "hail"
    located = located_frame(combined)
    located.loc[1, "unit"] = "Dorchster"
    names = failed(run_processed(write_processed(tmp_path, combined, located)))
    assert names == {
        ("311_combined", "expect_column_values_to_be_in_set", "storm_group"),
        ("311_located", "expect_column_values_to_be_in_set", "unit"),
    }


def test_duplicate_case_and_future_date_fail(tmp_path):
    combined = combined_frame()
    combined.loc[1, "case_id"] = "C0"
    combined.loc[2, "open_dt"] = pd.Timestamp("2027-01-01")
    names = failed(run_processed(write_processed(tmp_path, combined, located_frame(combined))))
    assert ("311_combined", "expect_column_values_to_be_unique", "case_id") in names
    assert ("311_combined", "expect_column_values_to_be_between", "open_dt") in names


def test_request_at_midnight_after_the_latest_day_fails(tmp_path):
    combined = combined_frame()
    combined.loc[0, "open_dt"] = pd.Timestamp("2026-10-26 00:00")  # latest is 2026-10-25
    names = failed(run_processed(write_processed(tmp_path, combined, located_frame(combined))))
    assert ("311_combined", "expect_column_values_to_be_between", "open_dt") in names


def test_a_short_year_is_left_out_of_the_placed_share(tmp_path):
    # 1,200 placed rows in 2024, then 20 rows in early 2025 with half unplaced.
    combined = combined_frame(n=1220)
    combined.loc[1200:, "open_dt"] = pd.Timestamp("2025-01-02 12:00")
    located = located_frame(combined)
    located.loc[1210:, ["location", "unit", "geoid20"]] = ["no_coordinates", None, None]
    assert not run_processed(write_processed(tmp_path, combined, located)).failed


def test_low_placed_share_and_lost_rows_fail(tmp_path):
    combined = combined_frame(n=1200)
    located = located_frame(combined, placed=0.9).iloc[:-1]
    names = failed(run_processed(write_processed(tmp_path, combined, located)))
    assert names == {
        ("311_located", "expect_table_row_count_to_equal", ""),
        ("311_located per year", "expect_column_min_to_be_between", "placed_share"),
    }


def test_missing_processed_file_fails(tmp_path):
    names = failed(run_processed((tmp_path / "311_combined.parquet", tmp_path / "311_located.parquet")))
    assert names == {("311_combined.parquet", "file_exists", ""), ("311_located.parquet", "file_exists", "")}


def test_empty_combined_table_fails(tmp_path):
    combined = combined_frame().iloc[:0]
    names = failed(run_processed(write_processed(tmp_path, combined, located_frame(combined))))
    assert ("311_combined", "expect_table_row_count_to_be_between", "") in names


# Bounds file and the command line

def test_bounds_file_needs_every_bound(tmp_path):
    path = tmp_path / "bounds.csv"
    path.write_text("name,value\nopen_dt_parsed_min,0.999\n")
    with pytest.raises(ValueError, match="missing bounds"):
        load_bounds(path)


def test_bounds_must_be_numbers(tmp_path):
    path = tmp_path / "bounds.csv"
    path.write_text(BOUNDS_FILE.read_text().replace("0.999", "high"))
    with pytest.raises(ValueError, match="not a number"):
        load_bounds(path)


def test_main_writes_results_and_raises_with_the_failed_checks(raw, tmp_path, monkeypatch):
    raw_dir, published = raw
    new_csv(raw_dir / "new_system.csv", shift_hours=5)
    published_path = tmp_path / "published.csv"
    published.to_csv(published_path, index=False)
    out = tmp_path / "validation" / "raw.json"
    monkeypatch.setattr(sys, "argv", [
        "validate_data.py", "--stage", "raw", "--config", str(BOUNDS_FILE), "--raw-dir", str(raw_dir),
        "--years", "2024", "--published", str(published_path), "--out", str(out),
    ])
    with pytest.raises(ValidationFailed, match=r"raw new_system.csv: expect_column_median_to_be_between\(open_hour\)"):
        main()
    result = json.loads(out.read_text())
    assert result["stage"] == "raw"
    assert sum(not c["success"] for c in result["checks"]) == 1


def test_main_processed_reads_units_from_the_neighborhoods(tmp_path, monkeypatch):
    combined = combined_frame()
    combined_path, located_path = write_processed(tmp_path, combined, located_frame(combined))
    hoods = tmp_path / "neighborhoods.parquet"
    gpd.GeoDataFrame({"neighborhood": UNITS}, geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)], crs="EPSG:4326").to_parquet(hoods)
    out = tmp_path / "processed.json"
    monkeypatch.setattr(sys, "argv", [
        "validate_data.py", "--stage", "processed", "--config", str(BOUNDS_FILE), "--combined", str(combined_path),
        "--located", str(located_path), "--neighborhoods", str(hoods), "--latest", "2026-10-25", "--out", str(out),
    ])
    main()
    assert all(c["success"] for c in json.loads(out.read_text())["checks"])
