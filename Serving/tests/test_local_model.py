import json

import pytest

import local_model


def make_run(tmp_path, **run):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "model.joblib").write_bytes(b"model bytes")
    record = {"model": "lightgbm", "run_id": "ci-lightgbm", "threshold": 0.5, "data_version": "sha256:" + "b" * 64,
              "mlflow": {"run_id": "c" * 32}}
    record.update(run)
    (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


@pytest.fixture
def outside(tmp_path, monkeypatch):
    monkeypatch.setattr(local_model, "REPO", tmp_path / "repo")


def test_writes_the_model_and_the_registry_keys(tmp_path, outside, capsys):
    out = tmp_path / "out"
    assert local_model.main([str(make_run(tmp_path)), str(out)]) == 0
    metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    # key names and nesting as Model-Development/scripts/registry.py build_push writes them
    assert set(metadata) <= {"model", "run_id", "data_version", "mlflow_run_id", "threshold", "files"}
    assert metadata["mlflow_run_id"] == "c" * 32 and metadata["threshold"] == 0.5
    assert metadata["files"] == {"model.joblib": local_model.sha256(out / "model.joblib")}
    assert (out / "model.joblib").read_bytes() == b"model bytes"
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 2 and all(line.startswith("sha256:") for line in printed)
    assert (out / "metadata.json").read_bytes() == local_model.json_bytes(metadata)  # registry's JSON form


def test_refuses_a_folder_inside_the_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(local_model, "REPO", tmp_path / "repo")
    run = make_run(tmp_path)
    with pytest.raises(SystemExit, match="refusing to write inside the repository"):
        local_model.main([str(run), str(tmp_path / "repo" / "models")])
    with pytest.raises(SystemExit, match="refusing"):
        local_model.main([str(run), str(tmp_path / "repo")])
    assert not (tmp_path / "repo").exists()


@pytest.mark.parametrize("change", [{"threshold": None}, {"mlflow": {}}, {"run_id": None}, {"model": None}])
def test_a_run_without_what_serving_reads_is_refused(tmp_path, outside, change):
    with pytest.raises(SystemExit, match="has no"):
        local_model.main([str(make_run(tmp_path, **change)), str(tmp_path / "out")])


def test_a_missing_model_or_run_file_is_refused(tmp_path, outside):
    run = make_run(tmp_path)
    (run / "model.joblib").unlink()
    with pytest.raises(SystemExit, match="model.joblib is missing"):
        local_model.main([str(run), str(tmp_path / "out")])
    (run / "model.joblib").write_bytes(b"x")
    (run / "run.json").write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit, match="cannot read"):
        local_model.main([str(run), str(tmp_path / "out")])


def test_usage(capsys):
    assert local_model.main(["--help"]) == 0
    assert local_model.main([]) == 2
    assert "local_model.py <run_dir> <out_dir>" in capsys.readouterr().out
