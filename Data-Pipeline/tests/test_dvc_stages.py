from pathlib import Path

import pytest

import dvc_stages
import pipeline_steps as steps


def outs_by_stage():
    return {name: [path for path, _ in spec["outs"]] for name, spec in dvc_stages.stages().items()}


def inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def test_the_committed_dvc_yaml_is_what_step_plan_gives():
    # A change to step_plan needs `python Data-Pipeline/scripts/dvc_stages.py` in the same PR.
    assert dvc_stages.DVC_YAML.read_text(encoding="utf-8") == dvc_stages.render(), (
        "Data-Pipeline/dvc.yaml is stale: run python Data-Pipeline/scripts/dvc_stages.py"
    )


def test_check_mode_passes_on_the_committed_file(capsys):
    dvc_stages.main(["--check"])
    assert "is current" in capsys.readouterr().out


def test_check_mode_fails_on_a_stale_file(tmp_path, monkeypatch):
    stale = tmp_path / "dvc.yaml"
    stale.write_text("stages: {}\n")
    monkeypatch.setattr(dvc_stages, "DVC_YAML", stale)
    with pytest.raises(SystemExit, match="is stale"):
        dvc_stages.main(["--check"])
    assert stale.read_text() == "stages: {}\n"


def test_every_real_step_is_a_stage_or_left_out_with_a_reason():
    real = {*steps.SCRIPTS, *steps.LOCAL}
    assert len(dvc_stages.STEPS) == len(set(dvc_stages.STEPS))
    assert not set(dvc_stages.STEPS) & set(dvc_stages.LEFT_OUT)
    assert set(dvc_stages.STEPS) | set(dvc_stages.LEFT_OUT) == real
    assert all(reason.strip() for reason in dvc_stages.LEFT_OUT.values())


def test_the_stages_cover_raw_processed_and_labels():
    outs = {path for paths in outs_by_stage().values() for path in paths}
    assert "data/raw/311/new_system.csv" in outs
    assert {"data/processed/daily_counts.parquet", "data/processed/labels.parquet",
            "data/processed/features.parquet"} <= outs


def test_each_stage_runs_the_dag_step_through_run_step():
    real = {*steps.SCRIPTS, *steps.LOCAL}
    for name, spec in dvc_stages.stages().items():
        words = spec["cmd"].split()
        assert words[:2] == ["python", "scripts/pipeline_steps.py"]
        assert words[2] in real
        if words[2] == "parse_legacy_311":
            assert words[3:] == ["--year", name.rsplit("_", 1)[1]]
        else:
            assert words[3:] == [] and name == words[2]


def test_one_parse_stage_per_legacy_year_then_the_merge():
    names = list(dvc_stages.stages())
    years = [f"parse_legacy_311_{year}" for year in steps.LEGACY_YEARS]
    assert [name for name in names if name.startswith("parse_legacy_311")] == years
    merge = dvc_stages.stages()["merge_legacy_311"]
    assert merge["deps"][-len(years):] == [f"data/interim/legacy_311_by_year/{year}.parquet" for year in steps.LEGACY_YEARS]


def test_no_out_overlaps_another_and_every_out_is_pipeline_data():
    flat = [(name, path) for name, paths in outs_by_stage().items() for path in paths]
    for i, (name, path) in enumerate(flat):
        assert path.startswith("data/") and not path.startswith(("data/state", "data/slices")), path
        for other_name, other in flat[i + 1:]:
            assert not inside(path, other) and not inside(other, path), (name, path, other_name, other)


def test_every_data_dep_comes_from_a_stage():
    # So a clean clone without the remote rebuilds everything from the public sources.
    outs = {path for paths in outs_by_stage().values() for path in paths}
    for name, spec in dvc_stages.stages().items():
        for dep in spec["deps"]:
            if dep.startswith("data/"):
                assert any(inside(dep, out) for out in outs), (name, dep)


def test_code_and_config_deps_are_in_the_repo():
    for spec in dvc_stages.stages().values():
        for dep in spec["deps"]:
            if not dep.startswith("data/"):
                assert (dvc_stages.PIPELINE / dep).exists(), dep


def test_code_deps_are_the_script_and_what_it_imports():
    feature = dvc_stages.stages()["feature_table"]["deps"]
    assert {"scripts/pipeline_steps.py", "scripts/feature_table.py", "scripts/surge_labels.py",
            "scripts/weather_features.py", "scripts/period_files.py"} <= set(feature)
    # pipeline_steps imports legacy_311 only inside a function, for row counts
    assert dvc_stages.stages()["ingest_tides"]["deps"] == ["scripts/pipeline_steps.py", "scripts/raw_fetch.py",
                                                           "scripts/tides.py"]
    assert "scripts/legacy_311.py" in dvc_stages.stages()["parse_legacy_311_2015"]["deps"]


def test_downloads_and_kept_outputs_persist():
    persisted = {path for spec in dvc_stages.stages().values() for path, persist in spec["outs"] if persist}
    assert {"data/raw/311/2015.csv", "data/raw/311/new_system.csv", "data/raw/weather/noaa_logan", "data/raw/tides",
            "data/raw/open_meteo", "data/raw/static", "data/raw/nws_alerts", "data/processed/stats"} <= persisted
    assert not {"data/interim/static", "data/interim/311_combined.parquet", "data/processed/labels.parquet"} & persisted


def test_static_layer_files_are_inside_the_folder_out():
    outs = dict(dvc_stages.stages()["ingest_static_layers"]["outs"])
    assert "data/interim/static" in outs
    assert not [path for path in outs if path.startswith("data/interim/static/")]


def test_dvc_yaml_parses_as_yaml_with_the_same_stages():
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(dvc_stages.render())
    assert list(parsed["stages"]) == list(dvc_stages.stages())
    assert parsed["stages"]["ingest_tides"]["outs"] == [{"data/raw/tides": {"persist": True}}]


def test_unusual_strings_are_quoted():
    assert dvc_stages.scalar("data/raw/311/2015.csv") == "data/raw/311/2015.csv"
    assert dvc_stages.scalar("a: b") == '"a: b"'
    assert dvc_stages.scalar("#x") == '"#x"'


def test_paths_are_relative_to_data_pipeline():
    assert dvc_stages.relative(dvc_stages.PIPELINE / "data" / "raw") == "data/raw"
    assert dvc_stages.relative(steps.REPO / "docs" / "data" / "category-map.csv") == "../docs/data/category-map.csv"


def test_the_command_line_runs_the_full_build_step(monkeypatch):
    calls = []
    monkeypatch.setattr(steps, "run_step", lambda step, days, **kw: calls.append((step, days, kw)))
    steps.main(["parse_legacy_311", "--year", "2016"])
    steps.main(["surge_labels"])
    assert calls == [
        ("parse_legacy_311", steps.DateRange(), {"year": 2016, "run_id": "dvc"}),
        ("surge_labels", steps.DateRange(), {"year": None, "run_id": "dvc"}),
    ]


@pytest.mark.parametrize("argv", [["surge_labels", "--year", "2016"], ["parse_legacy_311", "--year", "2014"], ["no_such_step"]])
def test_the_command_line_rejects_bad_arguments(argv, monkeypatch):
    monkeypatch.setattr(steps, "run_step", lambda *a, **kw: pytest.fail("ran"))
    with pytest.raises(SystemExit) as e:
        steps.main(argv)
    assert e.value.code == 2


def test_a_missing_input_exits_with_its_message(monkeypatch):
    def missing(*args, **kwargs):
        raise steps.InputError("surge_labels cannot start: missing x. Run daily_counts first.")

    monkeypatch.setattr(steps, "run_step", missing)
    with pytest.raises(SystemExit, match="cannot start: missing x"):
        steps.main(["surge_labels"])


def test_a_real_missing_input_stops_the_stage(tmp_path, monkeypatch):
    # run_step's own check, as in the DAG: nothing runs without the counts.
    monkeypatch.setattr(steps, "REPO", tmp_path)
    monkeypatch.setattr(steps.run_step, "__defaults__", (tmp_path, "manual", None))
    (tmp_path / "Data-Pipeline" / "config").mkdir(parents=True)
    with pytest.raises(SystemExit, match="surge_labels cannot start: missing"):
        steps.main(["surge_labels"])
    assert not Path(tmp_path / "Data-Pipeline" / "data" / "processed" / "labels.parquet").exists()
