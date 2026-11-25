import json
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests
from google.auth.exceptions import GoogleAuthError

from conftest import FIXTURES
from serving import forecasts

PARQUET = FIXTURES / "forecasts" / "2026-11-26.parquet"
RECORDS = json.loads((FIXTURES / "forecasts" / "2026-11-26.records.json").read_text(encoding="utf-8"))


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "forecasts"
    folder.mkdir()
    shutil.copy(PARQUET, folder / "2026-11-26.parquet")
    return folder


def test_a_forecast_is_the_records_fixture_exactly(make_client, folder):
    response = make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-26")
    assert response.status_code == 200
    assert response.json() == RECORDS
    assert [list(row) for row in response.json()] == [list(row) for row in RECORDS]  # same key order


@pytest.mark.parametrize("date", ["2026-13-01", "2026-02-30", "26-11-26", "2026-1-26", "tomorrow", "20261126",
                                  "2026-11-26T00"])
def test_a_bad_date_is_422(make_client, folder, date):
    response = make_client("web", forecasts_root=str(folder)).get(f"/forecasts/{date}")
    assert response.status_code == 422
    assert response.json()["detail"][0]["path"] == "/date"


def test_a_date_with_no_file_is_404(make_client, folder):
    assert make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-27").status_code == 404


def test_a_file_without_the_contract_metadata_is_500_with_the_reason(make_client, folder):
    table = pq.read_table(PARQUET).replace_schema_metadata(None)
    pq.write_table(table, folder / "2026-11-27.parquet")
    response = make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-27")
    assert response.status_code == 500
    assert "contract=forecast/v1" in response.json()["detail"]


def test_a_file_with_another_contract_is_refused(make_client, folder):
    table = pq.read_table(PARQUET).replace_schema_metadata({"contract": "forecast/v2"})
    pq.write_table(table, folder / "2026-11-27.parquet")
    assert make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-27").status_code == 500


def test_a_file_that_is_not_parquet_is_500(make_client, folder):
    (folder / "2026-11-27.parquet").write_bytes(b"nope")
    response = make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-27")
    assert response.status_code == 500 and "not readable parquet" in response.json()["detail"]


def test_a_folder_that_cannot_be_read_is_502(make_client, folder):
    (folder / "2026-11-27.parquet").mkdir()  # a directory where a file should be
    assert make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-27").status_code == 502


class Response:
    def __init__(self, status_code, content=b""):
        self.status_code, self.content = status_code, content


class FakeSession:
    """What AuthorizedSession gives: .get(url, timeout=...). Serves the parquet fixture under one object."""

    def __init__(self, objects):
        self.objects, self.urls = objects, []

    def get(self, url, timeout):
        assert timeout > 0
        self.urls.append(url)
        for name, data in self.objects.items():
            if url == f"https://storage.googleapis.com/storage/v1/b/the-bucket/o/{name}?alt=media":
                return Response(200, data)
        return Response(404)


def test_a_bucket_path_is_read_through_the_json_api(make_client):
    session = FakeSession({"forecasts%2F2026-11-26.parquet": PARQUET.read_bytes()})
    made = []
    client = make_client("web", forecasts_root="gs://the-bucket/forecasts/",
                         session_factory=lambda: made.append(1) or session)
    assert client.get("/forecasts/2026-11-26").json() == RECORDS
    assert client.get("/forecasts/2026-11-26").json() == RECORDS
    assert client.get("/forecasts/2026-11-27").status_code == 404
    assert len(made) == 1, "one session for every request"
    assert session.urls[0] == "https://storage.googleapis.com/storage/v1/b/the-bucket/o/forecasts%2F2026-11-26.parquet?alt=media"


def test_a_bucket_with_no_prefix(make_client):
    session = FakeSession({"2026-11-26.parquet": PARQUET.read_bytes()})
    client = make_client("web", forecasts_root="gs://the-bucket", session_factory=lambda: session)
    assert client.get("/forecasts/2026-11-26").json() == RECORDS


def test_a_refused_read_is_502(make_client):
    class Refusing:
        def get(self, url, timeout):
            return Response(403)

    client = make_client("web", forecasts_root="gs://the-bucket/x", session_factory=Refusing)
    response = client.get("/forecasts/2026-11-26")
    assert response.status_code == 502 and "status 403" in response.json()["detail"]


def test_a_call_that_cannot_reach_the_store_is_502(make_client):
    class Failing:
        def get(self, url, timeout):
            raise requests.ConnectionError("down")

    client = make_client("web", forecasts_root="gs://the-bucket/x", session_factory=Failing)
    assert client.get("/forecasts/2026-11-26").status_code == 502


def test_missing_credentials_are_502(make_client):
    def no_credentials():
        raise RuntimeError("no Application Default Credentials")

    client = make_client("web", forecasts_root="gs://the-bucket/x", session_factory=no_credentials)
    response = client.get("/forecasts/2026-11-26")
    assert response.status_code == 502 and "RuntimeError" in response.json()["detail"]


def test_an_unexpected_error_is_500_and_logged_with_its_type(make_client, log_lines):
    class Buggy:
        def get(self, url, timeout):
            raise RuntimeError("secret detail that must not be logged")

    client = make_client("web", forecasts_root="gs://the-bucket/x", session_factory=Buggy)
    response = client.get("/forecasts/2026-11-26")
    assert response.status_code == 500 and response.json() == {"detail": "internal error"}
    assert [json.loads(line) for line in log_lines] == [{"event": "forecast_error", "exception": "RuntimeError"}]


def test_an_unexpected_error_while_reading_the_file_is_500_and_logged(make_client, folder, log_lines, monkeypatch):
    def broken(data):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(forecasts, "records", broken)
    response = make_client("web", forecasts_root=str(folder)).get("/forecasts/2026-11-26")
    assert response.status_code == 500
    assert [json.loads(line) for line in log_lines] == [{"event": "forecast_error", "exception": "ZeroDivisionError"}]


def test_a_refused_token_is_502(make_client):
    class Refused:
        def get(self, url, timeout):
            raise GoogleAuthError("token refused")

    client = make_client("web", forecasts_root="gs://the-bucket/x", session_factory=Refused)
    assert client.get("/forecasts/2026-11-26").status_code == 502


def test_a_root_with_no_bucket_is_502(make_client):
    client = make_client("web", forecasts_root="gs:///forecasts", session_factory=lambda: None)
    assert client.get("/forecasts/2026-11-26").status_code == 502


def test_timestamps_and_dates_come_out_as_in_the_records_fixture():
    schema = pa.schema([("d", pa.date32()), ("t", pa.timestamp("us", tz="UTC"))])
    table = pa.table({"d": [pa.scalar(0, pa.date32()).as_py()], "t": [pa.scalar(1_000_000, pa.timestamp("us", tz="UTC")).as_py()]},
                     schema=schema).replace_schema_metadata({"contract": "forecast/v1"})
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    assert forecasts.records(sink.getvalue().to_pybytes()) == [{"d": "1970-01-01", "t": "1970-01-01T00:00:01Z"}]
