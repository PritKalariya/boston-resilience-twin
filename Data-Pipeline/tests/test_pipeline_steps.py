import json
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
INPUT_FLAGS = {"--raw-dir", "--raw", "--legacy", "--new", "--config", "--map"}


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
    assert outputs
    assert all(path.is_relative_to(run_paths(days, tmp_path).interim) for path in outputs)
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
    assert all(Path(v).is_absolute() for flag, v in values.items() if flag in OUTPUT_FLAGS | INPUT_FLAGS)


def test_versioned_inputs_exist_in_the_repo():
    for step in ("ingest_static_layers", "combine_311"):
        values = flag_values(script_args(step, FULL))
        for flag in ("--config", "--map"):
            if flag in values:
                assert Path(values[flag]).is_file(), values[flag]


def test_legacy_step_reads_only_the_years_in_range():
    args = script_args("parse_legacy_311", DateRange(date(2024, 3, 1), date(2025, 2, 1)))
    years = args[args.index("--years") + 1:args.index("--out")]
    assert years == ["2024", "2025"]


def test_unknown_step():
    with pytest.raises(ValueError, match="no script step 'spatial_join'"):
        script_args("spatial_join", FULL)


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


def test_311_steps_run_end_to_end_on_a_slice(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    data = root / "Data-Pipeline" / "data"
    (data / "raw" / "311").mkdir(parents=True)
    (data / "raw" / "311" / "2025.csv").write_text(LEGACY_2025)
    (data / "raw" / "311" / "new_system.csv").write_text(NEW_SYSTEM)
    (root / "docs" / "data").mkdir(parents=True)
    (root / "docs" / "data" / "category-map.csv").write_text(CATEGORY_MAP)
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
    code = "import sys, pipeline_steps; print(sorted({'pandas', 'geopandas', 'numpy'} & set(sys.modules)))"
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
