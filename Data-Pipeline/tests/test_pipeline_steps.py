import json
import logging
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import pipeline_steps as steps
from pipeline_steps import DateRange, run_paths, run_script, run_step, script_args

SCRIPTS_DIR = Path(steps.__file__).resolve().parent
FULL = DateRange()
SLICE = DateRange(date(2025, 1, 1), date(2025, 12, 31))
OUTPUT_FLAGS = {"--out", "--out-dir", "--manifest", "--change-points"}
INPUT_FLAGS = {"--raw-dir", "--raw", "--legacy", "--new", "--config", "--map", "--published"}
STATE_FLAGS = {"--state-dir"}


def flag_values(args):
    """{flag: value} for flags followed by a value."""
    return {flag: value for flag, value in zip(args, args[1:]) if flag.startswith("--") and not value.startswith("--")}


def test_defaults_are_the_full_build(tmp_path):
    days = DateRange.from_params({})
    assert days.is_full and days.label == "2015-01-01_latest"
    assert days.legacy_years == list(range(2015, 2027))
    assert run_paths(days, tmp_path).interim == tmp_path / "Data-Pipeline" / "data" / "interim"


def test_trigger_form_defaults_are_the_full_build():
    # The trigger form sends both params; a cleared end date arrives as None or "".
    assert DateRange.from_params({"start": "2015-01-01", "end": None}).is_full
    assert DateRange.from_params({"start": "2015-01-01", "end": ""}).is_full


def test_one_year_is_a_slice(tmp_path):
    days = DateRange.from_params({"start": "2025-01-01", "end": "2025-12-31"})
    assert not days.is_full and days.legacy_years == [2025]
    data = tmp_path / "Data-Pipeline" / "data"
    paths = run_paths(days, tmp_path)
    assert paths.interim == data / "slices" / "2025-01-01_2025-12-31" / "interim"
    assert paths.processed == data / "slices" / "2025-01-01_2025-12-31" / "processed"
    assert paths.raw == data / "raw"


def test_open_ended_slice():
    days = DateRange.from_params({"start": "2025-06-01"})
    assert days.label == "2025-06-01_latest"
    assert days.legacy_years == [2025, 2026]


def test_explicit_end_is_a_slice_even_from_the_first_day():
    assert not DateRange.from_params({"start": "2015-01-01", "end": "2026-10-17"}).is_full


def test_years_stop_at_the_last_legacy_file():
    assert DateRange.from_params({"start": "2026-01-01", "end": "2030-12-31"}).legacy_years == [2026]


@pytest.mark.parametrize(
    "params, message",
    [
        ({"start": "2014-06-01"}, "before 2015-01-01"),
        ({"start": "2025-06-01", "end": "2025-05-31"}, "end 2025-05-31 is before start 2025-06-01"),
        ({"start": "2025-13-01"}, "start must be a date"),
        ({"end": "01/02/2025"}, "end must be a date"),
        ({"start": 20250101}, "start must be a date"),
        ({"start": "2027-01-01"}, "no legacy 311 file"),
        ({"stat": "2025-01-01"}, r"unknown params \['stat'\]"),
    ],
)
def test_bad_ranges_fail(params, message):
    with pytest.raises(ValueError, match=message):
        DateRange.from_params(params)


@pytest.mark.parametrize("days", [FULL, SLICE], ids=["full", "slice"])
@pytest.mark.parametrize("step", sorted(steps.SCRIPTS))
def test_outputs_stay_in_the_run_folder(tmp_path, step, days):
    args = script_args(step, days, tmp_path)
    outputs = [Path(v) for flag, v in flag_values(args).items() if flag in OUTPUT_FLAGS]
    paths = run_paths(days, tmp_path)
    if step.startswith("ingest_") and not outputs:
        # A raw download (#7): it adds to the shared raw folder and keeps its
        # history, so it writes nothing else, in a slice or a full build.
        assert Path(flag_values(args)["--raw-dir"]).is_relative_to(paths.raw)
        return
    assert outputs
    assert all(path.is_relative_to(paths.interim) or path.is_relative_to(paths.processed) for path in outputs)
    assert not any("gs://" in arg for arg in args)


@pytest.mark.parametrize("step", sorted(steps.SCRIPTS))
def test_inputs_are_raw_files_run_outputs_or_versioned_config(tmp_path, step):
    paths = run_paths(SLICE, tmp_path)
    allowed = [paths.raw, paths.interim, tmp_path / "Data-Pipeline" / "config", tmp_path / "docs" / "data"]
    for flag, value in flag_values(script_args(step, SLICE, tmp_path)).items():
        if flag in INPUT_FLAGS:
            assert any(Path(value).is_relative_to(base) for base in allowed), (flag, value)


@pytest.mark.parametrize("step", sorted(steps.SCRIPTS))
def test_every_path_is_absolute(step):
    values = flag_values(script_args(step, SLICE))
    assert all(Path(v).is_absolute() for flag, v in values.items() if flag in OUTPUT_FLAGS | INPUT_FLAGS | STATE_FLAGS)


def test_versioned_inputs_exist_in_the_repo():
    for step in ("ingest_static_layers", "combine_311", "validate_raw", "validate_processed"):
        values = flag_values(script_args(step, FULL))
        for flag in ("--config", "--map", "--published"):
            if flag in values:
                assert Path(values[flag]).is_file(), values[flag]


def test_legacy_step_reads_only_the_years_in_range():
    args = script_args("parse_legacy_311", DateRange(date(2024, 3, 1), date(2025, 2, 1)))
    years = args[args.index("--years") + 1:args.index("--out")]
    assert years == ["2024", "2025"]


def test_unknown_step():
    with pytest.raises(ValueError, match="no script step 'labels_and_features'"):
        script_args("labels_and_features", FULL)


FAKE_SCRIPT = """
import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--fail", action="store_true")
    parser.add_argument("--exit", type=int)
    args = parser.parse_args()
    if args.fail:
        raise ValueError("bad input")
    if args.exit is not None:
        sys.exit(args.exit)
    Path(args.out).write_text(json.dumps(sys.argv))
"""


@pytest.fixture
def fake_script(tmp_path, monkeypatch):
    (tmp_path / "fake_step.py").write_text(FAKE_SCRIPT)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "fake_step"
    sys.modules.pop("fake_step", None)


def test_run_script_passes_args_and_restores_argv(tmp_path, fake_script):
    before = list(sys.argv)
    out = tmp_path / "argv.json"
    run_script(fake_script, ["--out", str(out)])
    assert json.loads(out.read_text()) == ["fake_step.py", "--out", str(out)]
    assert sys.argv == before


def test_run_script_reports_argparse_errors(fake_script):
    before = list(sys.argv)
    with pytest.raises(RuntimeError, match="fake_step.py exited with code 2"):
        run_script(fake_script, ["--no-such-flag"])
    assert sys.argv == before


def test_run_script_lets_errors_through(tmp_path, fake_script):
    before = list(sys.argv)
    with pytest.raises(ValueError, match="bad input"):
        run_script(fake_script, ["--out", str(tmp_path / "argv.json"), "--fail"])
    assert sys.argv == before


def test_run_script_treats_exit_zero_as_success(tmp_path, fake_script):
    run_script(fake_script, ["--out", str(tmp_path / "argv.json"), "--exit", "0"])


LEGACY_2025 = (
    "case_enquiry_id,open_dt,closed_dt,type,neighborhood,latitude,longitude\n"
    "101,2025-03-01 08:00:00,,Tree Emergencies,Roxbury,42.31,-71.09\n"
    "102,2025-03-02 09:00:00,,Tree Emergencies,Roxbury,42.31,-71.09\n"
)
NEW_SYSTEM = (
    "case_id,open_date,close_date,service_name,neighborhood,latitude,longitude\n"
    "BCS-1,2025-10-27 10:00:00+00,,Fallen Tree or Branches,Roxbury,42.31,-71.09\n"
)
CATEGORY_MAP = (
    "system,source_category,category,storm_group,match,note\n"
    "legacy,Tree Emergencies,Fallen Tree or Branches,tree,renamed,\n"
    "new,Fallen Tree or Branches,Fallen Tree or Branches,tree,renamed,\n"
)


def make_repo(root, legacy_by_year, new_system):
    """Raw 311 files and the category map, laid out as in the repo."""
    raw = root / "Data-Pipeline" / "data" / "raw" / "311"
    raw.mkdir(parents=True)
    for year, body in legacy_by_year.items():
        (raw / f"{year}.csv").write_text(body)
    (raw / "new_system.csv").write_text(new_system)
    (root / "docs" / "data").mkdir(parents=True)
    (root / "docs" / "data" / "category-map.csv").write_text(CATEGORY_MAP)
    return root / "Data-Pipeline" / "data"


def test_311_steps_run_end_to_end_on_a_slice(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    data = make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)  # the scripts' relative defaults must never be used

    for step in ("parse_legacy_311", "parse_new_311", "combine_311"):
        run_step(step, SLICE, root)

    interim = run_paths(SLICE, root).interim
    assert sorted(p.name for p in interim.iterdir()) == [
        "311_combined.parquet", "change-points.csv", "legacy_311.parquet", "new_311.parquet",
    ]
    combined = pd.read_parquet(interim / "311_combined.parquet")
    assert combined["source_system"].value_counts().to_dict() == {"legacy": 2, "new": 1}
    assert not (data / "interim").exists()  # the full-build folder is untouched
    assert list(workdir.iterdir()) == []


LEGACY_HEADER = "case_enquiry_id,open_dt,closed_dt,type,neighborhood,latitude,longitude,closure_reason\n"
TREE = "Tree Emergencies,Roxbury,42.31,-71.09"
NEW_HEADER = "case_id,open_date,close_date,service_name,neighborhood,latitude,longitude\n"
FALLEN = "Fallen Tree or Branches,Roxbury,42.31,-71.09"


def test_slice_matches_a_wider_run_for_its_days(tmp_path, monkeypatch):
    # The tree switch (first new case) is before the slice, and legacy case 302
    # was resubmitted as a new case opened after it: both must come out the same
    # as in the wider run, which is why the new-system export is never cut.
    root = tmp_path / "repo"
    make_repo(
        root,
        {
            2025: LEGACY_HEADER
            + f"201,2025-03-01 08:00:00,,{TREE},\n"
            + f"202,2025-12-31 23:30:00,,{TREE},\n",
            2026: LEGACY_HEADER
            + f"301,2026-01-01 00:00:00,,{TREE},\n"  # first second of the slice
            + f"302,2026-01-15 09:00:00,2026-02-03 10:00:00,{TREE},Resubmitted as BCS-42\n"
            + f"303,2026-01-31 23:59:59,,{TREE},\n"  # last second of the slice
            + f"304,2026-02-01 00:00:00,,{TREE},\n",  # first second after it
        },
        NEW_HEADER
        + f"BCS-00000041,2025-10-27 10:00:00+00,,{FALLEN}\n"  # the switch
        # Near midnight at both edges: Boston local times (ADR-020), so 44 is in the
        # slice and 45 is not. Read as UTC, both would move across the edges.
        + f"BCS-00000044,2026-01-01 02:00:00+00,,{FALLEN}\n"
        + f"BCS-00000043,2026-01-10 10:00:00+00,,{FALLEN}\n"
        + f"BCS-00000045,2026-02-01 03:00:00+00,,{FALLEN}\n"
        + f"BCS-00000042,2026-02-03 10:00:00+00,,{FALLEN}\n",  # 302's new case
    )
    monkeypatch.chdir(tmp_path)
    wide, narrow = DateRange(date(2025, 1, 1)), DateRange(date(2026, 1, 1), date(2026, 1, 31))
    for days in (wide, narrow):
        for step in ("parse_legacy_311", "parse_new_311", "combine_311"):
            run_step(step, days, root)

    def read(days, name):
        return pd.read_parquet(run_paths(days, root).interim / name)

    full = read(wide, "311_combined.parquet")
    january = (full["open_dt"] >= pd.Timestamp("2026-01-01")) & (full["open_dt"] < pd.Timestamp("2026-02-01"))
    sliced = read(narrow, "311_combined.parquet")
    pd.testing.assert_frame_equal(sliced, full[january].reset_index(drop=True))
    assert sliced["case_id"].astype(str).tolist() == ["301", "303", "BCS-00000044", "BCS-00000043"]
    assert sliced["after_switch"].tolist() == [True, True, False, False]
    assert read(narrow, "legacy_311.parquet")["case_enquiry_id"].astype(str).tolist() == ["301", "302", "303"]
    assert len(read(narrow, "new_311.parquet")) == 5  # the whole export, on purpose


def write_times(path, times):
    pd.DataFrame({"open_dt": pd.to_datetime(times), "case": range(len(times))}).to_parquet(path, index=False)


def test_trim_keeps_whole_days_at_both_ends(tmp_path):
    path = tmp_path / "rows.parquet"
    write_times(path, ["2024-12-31 23:59:59", "2025-01-01 00:00:00", "2025-06-30 23:59:59", "2025-07-01 00:00:00"])
    assert steps.trim_to_range(path, "open_dt", DateRange(date(2025, 1, 1), date(2025, 6, 30))) == (4, 2)
    assert pd.read_parquet(path)["case"].tolist() == [1, 2]


def test_trim_with_an_open_end_keeps_everything_after_the_start(tmp_path):
    path = tmp_path / "rows.parquet"
    write_times(path, ["2024-12-31 23:59:59", "2025-01-01 00:00:00", "2031-01-01 12:00:00"])
    assert steps.trim_to_range(path, "open_dt", DateRange(date(2025, 1, 1))) == (3, 2)


def test_trim_an_empty_file(tmp_path):
    path = tmp_path / "rows.parquet"
    write_times(path, [])
    assert steps.trim_to_range(path, "open_dt", SLICE) == (0, 0)
    assert pd.read_parquet(path).empty


@pytest.fixture
def skip_file_checks(monkeypatch):
    """Run run_step's control flow without real input or output files."""
    monkeypatch.setattr(steps, "check_inputs", lambda step, files: None)
    monkeypatch.setattr(steps, "check_outputs", lambda step, files: None)
    monkeypatch.setattr(steps, "count_rows", lambda path: 0)


def test_slices_trim_legacy_and_combined_but_not_the_new_export(tmp_path, monkeypatch, skip_file_checks):
    calls = []
    monkeypatch.setattr(steps, "run_script", lambda module, args: calls.append(module))
    monkeypatch.setattr(steps, "trim_to_range", lambda path, column, days: calls.append((path.name, column)) or (1, 1))
    for step in ("parse_legacy_311", "parse_new_311", "combine_311", "ingest_static_layers"):
        run_step(step, SLICE, tmp_path)
    assert calls == [
        "legacy_311", ("legacy_311.parquet", "open_dt"),
        "new_311",
        "map_311", ("311_combined.parquet", "open_dt"),
        "static_layers",
    ]


def test_the_full_build_is_never_trimmed(tmp_path, monkeypatch, skip_file_checks):
    monkeypatch.setattr(steps, "run_script", lambda module, args: None)
    monkeypatch.setattr(steps, "trim_to_range", lambda *args: pytest.fail("the full build keeps every row"))
    for step in steps.SCRIPTS:
        run_step(step, FULL, tmp_path)


def test_only_the_ingest_tasks_retry():
    assert steps.NETWORK == {
        "ingest_static_layers", "ingest_legacy_311", "ingest_new_311", "ingest_noaa_logan",
        "ingest_nws_forecasts", "ingest_nws_alerts", "ingest_tides", "ingest_open_meteo",
        "ingest_alert_history",
    }
    assert steps.RETRIES == 2
    assert steps.RETRY_DELAY.total_seconds() == 300 and steps.RETRY_BACKOFF == 2.0
    assert steps.MAX_RETRY_DELAY.total_seconds() == 900


@pytest.mark.parametrize("step", sorted(steps.SCRIPTS))
def test_step_files_are_the_files_in_the_arguments(tmp_path, step):
    args = script_args(step, SLICE, tmp_path)
    folders = [Path(v) for flag, v in flag_values(args).items() if flag in {"--raw-dir", "--out-dir"}]
    files = steps.step_files(step, SLICE, tmp_path)
    for path in files.data_in + files.config_in + files.data_out:
        assert str(path) in args or any(path.is_relative_to(folder) for folder in folders), path
    assert steps.step_plan(step, SLICE, tmp_path).hint


def test_spatial_join_reads_the_combined_table_and_the_static_layers(tmp_path):
    files = steps.step_files("spatial_join", SLICE, tmp_path)
    interim = run_paths(SLICE, tmp_path).interim
    assert files.data_in == [interim / "311_combined.parquet"]
    assert files.data_out == [interim / "311_located.parquet"]
    assert interim / "static" / "tracts_2020.parquet" in files.config_in


def test_static_outputs_are_the_configured_layers(tmp_path):
    config = tmp_path / "Data-Pipeline" / "config"
    config.mkdir(parents=True)
    (config / "static_layers.csv").write_text(
        "name,kind,file,url,publisher,vintage,license,key,text_columns,note\n"
        "tracts,vector,tracts.geojson,u,p,v,l,id,,\n"
        "population,table,population.csv,u,p,v,l,id,,\n"
        "dictionary,document,dictionary.xlsx,u,p,v,l,,,\n"
    )
    out = run_paths(SLICE, tmp_path).interim / "static"
    assert steps.step_files("ingest_static_layers", SLICE, tmp_path).data_out == [out / "tracts.parquet", out / "population.parquet"]


@pytest.mark.parametrize(
    "body",
    [
        'a,b\n1,"two\nlines"\n3,4\n',  # a quoted line break: falls back to the CSV reader
        'a,b\n1,"say ""hi"""\n\n3,4\n',  # escaped quotes stay even; an empty line is no record
        "a,b\r\n1,2\r\n   \r\n3,4",  # CRLF, a line of spaces is a record, no final line break
        "a,b\n",
        "",
    ],
)
def test_csv_records_match_the_parsers_count(tmp_path, body):
    from legacy_311 import count_records

    path = tmp_path / "rows.csv"
    path.write_bytes(body.encode())
    assert steps.count_csv_records(path) == count_records(path)


def test_count_rows_of_csv_records_and_parquet(tmp_path):
    (tmp_path / "rows.csv").write_text("a,b\n1,2\n3,4\n")
    (tmp_path / "no_final_break.csv").write_text("a,b\n1,2")
    (tmp_path / "header_only.csv").write_text("a,b\n")
    (tmp_path / "empty.csv").write_bytes(b"")
    (tmp_path / "line_break.csv").write_text('a,b\n1,"two\nlines"\n3,4\n')  # one record holds a line break
    pd.DataFrame({"x": [1, 2, 3]}).to_parquet(tmp_path / "one.parquet")
    names = ["rows.csv", "no_final_break.csv", "header_only.csv", "empty.csv", "line_break.csv"]
    assert [steps.count_rows(tmp_path / name) for name in names] == [2, 1, 0, 0, 2]
    assert steps.count_rows(tmp_path / "one.parquet") == 3


def test_every_task_logs_run_id_rows_in_and_rows_out(caplog):
    with caplog.at_level(logging.INFO, logger="pipeline_steps"):
        steps.log_task("r1", "daily_counts", 0, 0)
        steps.log_task("r1", "check_range", None, None)
        steps.log_task("r1", "combine_311", 348466, 280044, 2.31)
    assert caplog.messages == [
        "run r1 task daily_counts: rows in 0, rows out 0",
        "run r1 task check_range: rows in n/a, rows out n/a",
        "run r1 task combine_311: rows in 348,466, rows out 280,044 in 2.3 s",
    ]


def test_run_step_logs_its_rows(tmp_path, monkeypatch, caplog):
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="pipeline_steps"):
        assert run_step("parse_legacy_311", SLICE, root, run_id="test-run") == (2, 2)
        assert run_step("parse_new_311", SLICE, root, run_id="test-run") == (1, 1)
    assert caplog.messages[0] == "run test-run task parse_legacy_311: kept 2 of 2 rows of legacy_311.parquet in 2025-01-01_2025-12-31"
    assert re.fullmatch(r"run test-run task parse_legacy_311: rows in 2, rows out 2 in \d+\.\d s", caplog.messages[1])
    assert re.fullmatch(r"run test-run task parse_new_311: rows in 1, rows out 1 in \d+\.\d s", caplog.messages[2])


# The three failure cases of #36: missing source, corrupt file, empty download.

def test_missing_source_fails_before_the_script_runs(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    make_repo(root, {}, NEW_SYSTEM)  # no 2025.csv
    monkeypatch.setattr(steps, "run_script", lambda *args: pytest.fail("the script must not run"))
    with pytest.raises(steps.InputError, match=r"parse_legacy_311 cannot start: missing .*2025\.csv\. Put the yearly legacy 311 files"):
        run_step("parse_legacy_311", SLICE, root, run_id="test-run")


def test_corrupt_csv_fails_with_the_run_and_task_in_the_log(tmp_path, monkeypatch, caplog):
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    (root / "Data-Pipeline" / "data" / "raw" / "311" / "new_system.csv").write_bytes(bytes(range(256)) * 4)
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="pipeline_steps"), pytest.raises(ValueError):
        run_step("parse_new_311", SLICE, root, run_id="test-run")
    assert caplog.messages[-1].startswith("run test-run task parse_new_311 failed: ")


def test_corrupt_parquet_fails_with_the_run_and_task_in_the_log(tmp_path, monkeypatch, caplog):
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    interim = run_paths(SLICE, root).interim
    interim.mkdir(parents=True)
    for name in ("legacy_311.parquet", "new_311.parquet"):
        (interim / name).write_bytes(b"not a parquet file")
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="pipeline_steps"), pytest.raises(ValueError):
        run_step("combine_311", SLICE, root, run_id="test-run")
    assert caplog.messages[-1].startswith("run test-run task combine_311 failed: ")


def test_empty_download_fails_before_the_script_runs(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, "")  # a 0-byte new-system export
    monkeypatch.setattr(steps, "run_script", lambda *args: pytest.fail("the script must not run"))
    with pytest.raises(steps.InputError, match=r"parse_new_311 cannot start: empty .*new_system\.csv"):
        run_step("parse_new_311", SLICE, root, run_id="test-run")


def make_static_repo(root, raw_bytes):
    (root / "Data-Pipeline" / "config").mkdir(parents=True)
    (root / "Data-Pipeline" / "config" / "static_layers.csv").write_text(STATIC_CONFIG)
    raw = root / "Data-Pipeline" / "data" / "raw" / "static"
    raw.mkdir(parents=True)
    (raw / "population.csv").write_bytes(raw_bytes)


def test_an_empty_static_download_is_fetched_again(tmp_path, monkeypatch, caplog):
    import static_layers

    fetched = []

    def download(url, dest):
        fetched.append(url)
        dest.write_text("id,people\nA,10\n")

    monkeypatch.setattr(static_layers, "download", download)
    root = tmp_path / "repo"
    make_static_repo(root, b"")  # what an empty download leaves behind
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="pipeline_steps"):
        assert run_step("ingest_static_layers", SLICE, root, run_id="test-run") == (None, 1)
    assert fetched == ["https://example.org/population.csv"]
    assert "deleted the empty download" in caplog.messages[0]


def test_a_download_that_stays_empty_fails_clearly(tmp_path, monkeypatch, caplog):
    import static_layers

    monkeypatch.setattr(static_layers, "download", lambda url, dest: dest.write_bytes(b""))
    root = tmp_path / "repo"
    make_static_repo(root, b"")
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="pipeline_steps"), pytest.raises(ValueError, match="population.csv is empty"):
        run_step("ingest_static_layers", SLICE, root, run_id="test-run")
    assert caplog.messages[-1] == "run test-run task ingest_static_layers failed: ValueError: population.csv is empty"


def test_a_step_that_writes_nothing_fails(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    monkeypatch.setattr(steps, "run_script", lambda module, args: None)
    with pytest.raises(RuntimeError, match=r"parse_new_311 finished but wrote no .*new_311\.parquet"):
        run_step("parse_new_311", SLICE, root, run_id="test-run")


def test_a_stale_output_cannot_pass_for_new_output(tmp_path, monkeypatch):
    # Yesterday's 999-row file is in place and the script writes nothing.
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    interim = run_paths(SLICE, root).interim
    interim.mkdir(parents=True)
    pd.DataFrame({"case_id": range(999)}).to_parquet(interim / "new_311.parquet")
    monkeypatch.setattr(steps, "run_script", lambda module, args: None)
    with pytest.raises(RuntimeError, match=r"parse_new_311 finished but wrote no .*new_311\.parquet"):
        run_step("parse_new_311", SLICE, root, run_id="test-run")
    assert not (interim / "new_311.parquet").exists()


def test_a_counting_failure_is_logged_as_a_failure(tmp_path, monkeypatch, caplog):
    root = tmp_path / "repo"
    make_repo(root, {2025: LEGACY_2025}, NEW_SYSTEM)
    monkeypatch.chdir(tmp_path)

    def broken(path):
        raise OSError("disk went away")

    monkeypatch.setattr(steps, "count_rows", broken)
    with caplog.at_level(logging.ERROR, logger="pipeline_steps"), pytest.raises(OSError):
        run_step("parse_new_311", SLICE, root, run_id="test-run")
    assert caplog.messages[-1] == "run test-run task parse_new_311 failed: OSError: disk went away"


STATIC_CONFIG = (
    "name,kind,file,url,publisher,vintage,license,key,text_columns,note\n"
    "population,table,population.csv,https://example.org/population.csv,City,2025,PDDL,id,,\n"
)


def test_static_step_runs_on_a_slice_without_downloading(tmp_path, monkeypatch):
    import static_layers

    monkeypatch.setattr(static_layers, "download", lambda *a, **k: pytest.fail("the file is present; nothing to download"))
    root = tmp_path / "repo"
    (root / "Data-Pipeline" / "config").mkdir(parents=True)
    (root / "Data-Pipeline" / "config" / "static_layers.csv").write_text(STATIC_CONFIG)
    raw = root / "Data-Pipeline" / "data" / "raw" / "static"
    raw.mkdir(parents=True)
    (raw / "population.csv").write_text("id,people\nA,10\nB,20\n")
    monkeypatch.chdir(tmp_path)

    run_step("ingest_static_layers", SLICE, root)

    interim = run_paths(SLICE, root).interim
    assert pd.read_parquet(interim / "static" / "population.parquet")["people"].tolist() == [10, 20]
    assert pd.read_csv(interim / "static-layers.csv")["name"].tolist() == ["population"]


def test_every_placeholder_names_an_issue():
    assert all(re.fullmatch(r"#\d+", issue) for issue in steps.PLANNED.values())
    assert not set(steps.PLANNED) & set(steps.SCRIPTS)


def test_real_steps_are_scripts():
    for module in steps.SCRIPTS.values():
        assert (SCRIPTS_DIR / f"{module}.py").is_file(), module


def test_import_is_light():
    # The DAG imports pipeline_steps at parse time, so it must not pull in pandas or geopandas.
    code = "import sys, alerts, pipeline_steps; print(sorted({'pandas', 'geopandas', 'numpy', 'pyarrow'} & set(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], cwd=SCRIPTS_DIR, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_scripts_do_not_shadow_installed_packages():
    # PYTHONPATH puts Data-Pipeline/scripts ahead of the standard library and site-packages
    # in every Airflow process, so a script named like a package would replace it.
    taken = set(sys.stdlib_module_names) | {
        "airflow", "pendulum", "pandas", "numpy", "pyarrow", "geopandas", "shapely", "pyproj", "pyogrio",
        "great_expectations", "fairlearn", "dvc", "evidently", "mlflow", "sklearn", "lightgbm", "xgboost",
    }
    names = {path.stem for path in SCRIPTS_DIR.glob("*.py")}
    assert not names & taken


def test_labels_and_features_read_the_full_builds_counts_and_write_the_run_folder(tmp_path):
    # #16: labels need the 3 years before each day (ADR-005) and features the 28 days before,
    # which a slice's own counts do not hold.
    full_counts = run_paths(FULL, tmp_path).processed / "daily_counts.parquet"
    slice_paths = run_paths(SLICE, tmp_path)
    for step, out in (("surge_labels", "labels.parquet"), ("feature_table", "features.parquet")):
        files = steps.step_files(step, SLICE, tmp_path)
        assert files.data_in[0] == full_counts
        assert files.data_out == [slice_paths.processed / out]
        values = flag_values(script_args(step, SLICE, tmp_path))
        assert (values["--start"], values["--end"]) == ("2025-01-01", "2025-12-31")
    features = steps.step_files("feature_table", SLICE, tmp_path)
    assert slice_paths.interim / "nws_alerts.parquet" in features.data_in
    assert slice_paths.interim / "static" in features.config_in


def test_alert_history_downloads_into_the_shared_raw_folder(tmp_path):
    plan = steps.step_plan("ingest_alert_history", SLICE, tmp_path)
    assert "--download" in plan.args
    assert plan.refetch == run_paths(SLICE, tmp_path).raw / "nws_alerts"
    assert plan.files.data_out == [run_paths(SLICE, tmp_path).interim / "nws_alerts.parquet"]


def test_16_versioned_inputs_exist_in_the_repo():
    assert Path(flag_values(script_args("ingest_alert_history", FULL))["--map"]).is_file()
    for step in ("surge_labels", "feature_table"):
        values = flag_values(script_args(step, FULL))
        for flag in ("--rules", "--settings"):
            if flag in values:
                assert Path(values[flag]).is_file(), values[flag]
