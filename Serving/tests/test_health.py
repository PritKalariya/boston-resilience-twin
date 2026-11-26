import hashlib
import json

import joblib
import pytest

from conftest import MLFLOW_RUN_ID, MODEL_VERSION, THRESHOLD, copy_model


def test_health_is_200_with_a_verified_model(served):
    response = served.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model_version": MODEL_VERSION, "model_run_id": MLFLOW_RUN_ID}


def test_health_is_503_with_no_model_folder(make_client, tmp_path):
    response = make_client("model", model_dir=tmp_path / "empty").get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert "metadata.json" in response.json()["reason"]


def test_a_tampered_model_is_not_loaded_and_never_unpickled(make_client, model_dir, tmp_path, monkeypatch):
    folder = copy_model(model_dir, tmp_path / "tampered")
    with (folder / "model.joblib").open("ab") as stream:
        stream.write(b"\x00")
    calls = []
    monkeypatch.setattr(joblib, "load", lambda *a, **k: calls.append(a))
    response = make_client("model", model_dir=folder).get("/health")
    assert response.status_code == 503
    assert "does not match the sha256" in response.json()["reason"]
    assert calls == [], "joblib.load ran on bytes that failed the hash check"


def test_a_missing_model_file_is_not_loaded(make_client, model_dir, tmp_path):
    folder = copy_model(model_dir, tmp_path / "gone")
    (folder / "model.joblib").unlink()
    response = make_client("model", model_dir=folder).get("/health")
    assert response.status_code == 503
    assert "model.joblib is missing" in response.json()["reason"]


def edit_metadata(folder, **changes):
    path = folder / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is KeyError:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    path.write_text(json.dumps(metadata), encoding="utf-8")


def set_threshold(folder, value):
    """Make the model's embedded threshold and metadata.json's both value, with the hash kept in step."""
    saved = joblib.load(folder / "model.joblib")
    saved.threshold = value
    joblib.dump(saved, folder / "model.joblib")
    edit_metadata(folder, threshold=value, files={"model.joblib": "sha256:" + hashlib.sha256(
        (folder / "model.joblib").read_bytes()).hexdigest()})


@pytest.mark.parametrize("changes, reason", [
    ({"files": {}}, "lists no sha256 for model.joblib"),
    ({"files": KeyError}, "lists no sha256 for model.joblib"),
    ({"files": {"model.joblib": "md5:abc"}}, "no sha256:<64 hex> value"),
    ({"files": {"../model.joblib": "sha256:" + "0" * 64, "model.joblib": "sha256:" + "0" * 64}}, "not a plain name"),
    ({"mlflow_run_id": "ci-lightgbm"}, "mlflow_run_id"),
    ({"mlflow_run_id": KeyError}, "mlflow_run_id"),
    ({"threshold": 1.5}, "threshold"),
    ({"threshold": True}, "threshold"),
    ({"threshold": None}, "threshold"),
])
def test_bad_metadata_leaves_the_model_unloaded(make_client, model_dir, tmp_path, changes, reason):
    folder = copy_model(model_dir, tmp_path / "meta")
    edit_metadata(folder, **changes)
    response = make_client("model", model_dir=folder).get("/health")
    assert response.status_code == 503
    assert reason in response.json()["reason"]


def test_metadata_that_is_not_json_or_not_an_object(make_client, model_dir, tmp_path):
    for text, reason in (("{", "cannot be read as JSON"), ("[]", "not a JSON object")):
        folder = copy_model(model_dir, tmp_path / f"meta{len(text)}")
        (folder / "metadata.json").write_text(text, encoding="utf-8")
        response = make_client("model", model_dir=folder).get("/health")
        assert response.status_code == 503 and reason in response.json()["reason"]


def test_files_the_image_does_not_ship_are_not_checked(make_client, model_dir, tmp_path):
    """A registry metadata.json lists run.json, metrics.json and more; only model.joblib is shipped."""
    folder = copy_model(model_dir, tmp_path / "registry")
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    metadata["files"]["run.json"] = "sha256:" + "1" * 64
    (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    assert make_client("model", model_dir=folder).get("/health").status_code == 200
    (folder / "run.json").write_text("{}", encoding="utf-8")  # shipped now, so it is checked
    response = make_client("model", model_dir=folder).get("/health")
    assert response.status_code == 503 and "run.json does not match" in response.json()["reason"]


def test_a_model_version_that_is_not_a_registry_version_id_is_refused(make_client, model_dir):
    response = make_client("model", model_dir=model_dir, model_version="ci-lightgbm").get("/health")
    assert response.status_code == 503 and "MODEL_VERSION" in response.json()["reason"]
    assert make_client("model", model_dir=model_dir, model_version="0").get("/health").status_code == 200


def test_a_file_that_is_not_a_model_is_refused_after_the_hash_check(make_client, model_dir, tmp_path):
    folder = copy_model(model_dir, tmp_path / "junk")
    (folder / "model.joblib").write_bytes(b"not a pickle")
    edit_metadata(folder, files={"model.joblib": "sha256:" + hashlib.sha256(b"not a pickle").hexdigest()})
    response = make_client("model", model_dir=folder).get("/health")
    assert response.status_code == 503 and "cannot be loaded" in response.json()["reason"]


def test_web_mode_is_healthy_without_a_model(make_client, tmp_path):
    response = make_client("web", model_dir=tmp_path / "empty").get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_a_metadata_threshold_that_differs_from_the_models_is_refused(make_client, model_dir, tmp_path):
    folder = copy_model(model_dir, tmp_path / "differs")
    edit_metadata(folder, threshold=THRESHOLD + 1e-12)  # not hashed, so only the comparison can catch it
    handler_lines = []
    import logging

    class Capture(logging.Handler):
        def emit(self, record):
            handler_lines.append(record.getMessage())

    log = logging.getLogger("serving")
    log.addHandler(Capture())
    try:
        response = make_client("model", model_dir=folder).get("/health")
    finally:
        log.handlers = [h for h in log.handlers if not isinstance(h, Capture)]
    assert response.status_code == 503
    assert "differs from the model's embedded threshold" in response.json()["reason"]
    assert any("model_not_loaded" in line and "differs" in line for line in handler_lines)


def test_a_model_with_no_embedded_threshold_is_refused(make_client, model_dir, tmp_path):
    folder = copy_model(model_dir, tmp_path / "bare")
    saved = joblib.load(folder / "model.joblib")
    joblib.dump(saved.estimator.estimator, folder / "model.joblib")  # the bare LightGBM model
    edit_metadata(folder, files={"model.joblib": "sha256:" + hashlib.sha256(
        (folder / "model.joblib").read_bytes()).hexdigest()})
    response = make_client("model", model_dir=folder).get("/health")
    assert response.status_code == 503
    assert "no embedded threshold" in response.json()["reason"]
