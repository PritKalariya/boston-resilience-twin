"""Tests for the Vertex AI Model Registry REST client (#111): a fake requests session, no network."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import registry as R  # noqa: E402
import vertex_client as V  # noqa: E402

MODEL = "projects/p/locations/us-east1/models/42"
BASE = "https://us-east1-aiplatform.googleapis.com/v1"


class Response:
    def __init__(self, status: int, body=None, content: bytes | None = None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else (b"" if body is None else json.dumps(body).encode())
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class Session:
    """Answers each request with the next scripted response and records the call."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append(copy.deepcopy({"method": method, "url": url, **kwargs}))  # as sent, not as later changed
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def client():
    return V.VertexClient(R.load_settings(), session=Session(), sleep=lambda seconds: None)


def answer(client, *responses) -> Session:
    client.session = Session(*responses)
    return client.session


def test_upload_polls_the_operation(client):
    calls = answer(client, Response(200, {"name": "op/1", "done": False}),
                   Response(200, {"name": "op/1", "done": True, "response": {"model": MODEL, "modelVersionId": "3"}}))
    assert client.upload_version({"displayName": "surge-forecast"}, MODEL) == (MODEL, "3")
    first = calls.calls[0]
    assert first["url"] == f"{BASE}/projects/boston-resilience-twin-p2/locations/us-east1/models:upload"
    assert first["json"] == {"model": {"displayName": "surge-forecast"}, "parentModel": MODEL}
    assert calls.calls[1]["url"] == f"{BASE}/op/1"


def test_first_upload_has_no_parent(client):
    calls = answer(client, Response(200, {"done": True, "response": {"model": MODEL, "modelVersionId": 1}}))
    assert client.upload_version({}, None) == (MODEL, "1")
    assert "parentModel" not in calls.calls[0]["json"]


def test_a_failed_operation_is_an_error(client):
    answer(client, Response(200, {"name": "op/2", "done": True, "error": {"message": "bad container"}}))
    with pytest.raises(R.RegistryError, match="bad container"):
        client.upload_version({}, None)


def test_an_operation_that_never_finishes_times_out(client, monkeypatch):
    clock = iter([0, 0, V.OPERATION_TIMEOUT_SECONDS + 1])
    monkeypatch.setattr(V.time, "monotonic", lambda: next(clock))
    answer(client, Response(200, {"name": "op/3", "done": False}), Response(200, {"name": "op/3", "done": False}))
    with pytest.raises(R.RegistryError, match="not done after"):
        client.upload_version({}, None)


def test_upload_response_without_a_version_is_an_error(client):
    answer(client, Response(200, {"done": True, "response": {"model": MODEL}}))
    with pytest.raises(R.RegistryError, match="no model or modelVersionId"):
        client.upload_version({}, None)


def test_find_model_follows_pages_and_filters_by_name(client):
    calls = answer(client, Response(200, {"models": [{"name": MODEL}], "nextPageToken": "t"}), Response(200, {}))
    assert client.find_model() == MODEL
    assert calls.calls[0]["params"] == {"filter": 'display_name="surge-forecast"'}
    assert calls.calls[1]["params"]["pageToken"] == "t"


def test_two_models_with_the_name_is_an_error(client):
    answer(client, Response(200, {"models": [{"name": "a"}, {"name": "b"}]}))
    with pytest.raises(R.RegistryError, match="2 registry models"):
        client.find_model()


def test_no_model_is_none(client):
    answer(client, Response(200, {}))
    assert client.find_model() is None


def test_versions_and_aliases(client):
    versions = {"models": [{"versionId": "1", "versionAliases": ["production"]}, {"versionId": "2"}]}
    answer(client, Response(200, versions))
    assert client.find_alias(MODEL, "production")["versionId"] == "1"
    answer(client, Response(200, versions))
    assert client.find_alias(MODEL, "previous") is None
    calls = answer(client, Response(200, {"versionId": "1"}))
    assert client.get_version(MODEL, "production") == {"versionId": "1"}
    assert calls.calls[0]["url"] == f"{BASE}/{MODEL}@production"


def test_merge_aliases_adds_and_removes_in_one_call(client):
    calls = answer(client, Response(200, {}))
    client.merge_aliases(MODEL, "3", add=("production",), remove=("candidate",))
    assert calls.calls[0]["url"] == f"{BASE}/{MODEL}@3:mergeVersionAliases"
    assert calls.calls[0]["json"] == {"versionAliases": ["production", "-candidate"]}
    with pytest.raises(R.RegistryError, match="needs an alias"):
        client.merge_aliases(MODEL, "3")


@pytest.mark.parametrize("status", [401, 403])
def test_refused_credentials_are_no_access(client, status):
    answer(client, Response(status, {"error": {"message": "denied"}}))
    with pytest.raises(V.NoAccess, match="denied"):
        client.get_version(MODEL, "1")


def test_other_refusals_carry_their_status(client):
    answer(client, Response(500, content=b"<html>oops</html>"))
    with pytest.raises(V.HttpError) as caught:
        client.get_version(MODEL, "1")
    assert caught.value.status == 500 and "oops" in str(caught.value)


def test_a_request_that_does_not_complete_is_an_error(client):
    import requests

    answer(client, requests.ConnectionError("reset"))
    with pytest.raises(R.RegistryError, match="did not complete"):
        client.get_version(MODEL, "1")


def test_upload_file_never_overwrites(client, tmp_path):
    local = tmp_path / "model.joblib"
    local.write_bytes(b"model")
    calls = answer(client, Response(200, {"name": "x"}))
    uri = client.upload_file(local, "surge-forecast/run/model.joblib")
    assert uri == "gs://boston-resilience-twin-p2-models/surge-forecast/run/model.joblib"
    assert calls.calls[0]["params"]["ifGenerationMatch"] == "0" and calls.calls[0]["data"] == b"model"
    answer(client, Response(412, {"error": {"message": "precondition"}}))
    with pytest.raises(R.RegistryError, match="never overwritten"):
        client.upload_file(local, "surge-forecast/run/model.joblib")
    answer(client, Response(500, {"error": {"message": "boom"}}))
    with pytest.raises(V.HttpError):
        client.upload_file(local, "surge-forecast/run/model.joblib")


def test_download_file_writes_the_bytes(client, tmp_path):
    calls = answer(client, Response(200, content=b"\x80binary"))
    out = client.download_file("surge-forecast/run/model.joblib", tmp_path / "m")
    assert out.read_bytes() == b"\x80binary"
    assert calls.calls[0]["url"].endswith("/o/surge-forecast%2Frun%2Fmodel.joblib")
    assert calls.calls[0]["params"] == {"alt": "media"}


def test_no_credentials_is_no_access(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def none(**kwargs):
        raise DefaultCredentialsError("not found")

    monkeypatch.setattr(google.auth, "default", none)
    with pytest.raises(V.NoAccess, match="no Google credentials"):
        V.VertexClient(R.load_settings())


def test_calls_are_billed_to_project_2(monkeypatch):
    import google.auth

    seen = {}

    def fake_default(scopes=None, quota_project_id=None):
        seen.update(scopes=scopes, quota=quota_project_id)
        from google.auth.credentials import AnonymousCredentials

        return AnonymousCredentials(), None

    monkeypatch.setattr(google.auth, "default", fake_default)
    V.VertexClient(R.load_settings())
    assert seen == {"scopes": list(V.SCOPES), "quota": "boston-resilience-twin-p2"}
