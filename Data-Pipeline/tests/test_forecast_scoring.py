import copy
import importlib.util
import json
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests
from bucket_fakes import fake_bucket

import forecast_scoring as fs
import live_manifest as lm
from evening_run import Settings

REPO_ROOT = Path(fs.__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "docs" / "contracts" / "fixtures"
ROWS = FIXTURES / "forecast-rows" / "2026-11-25" / "features.parquet"
REQUEST = json.loads((FIXTURES / "predict" / "request.json").read_text(encoding="utf-8"))
RESPONSE = json.loads((FIXTURES / "predict" / "response.json").read_text(encoding="utf-8"))
FORECAST = FIXTURES / "forecasts" / "2026-11-26.parquet"
AS_OF = date(2026, 11, 25)
ISSUED = datetime(2026, 11, 25, 23, 41, 7, tzinfo=timezone.utc)
FAST = Settings("http://127.0.0.1:1", 5, (30, 60, 120, 240))


def build_fixtures():
    spec = importlib.util.spec_from_file_location("build_fixtures", REPO_ROOT / "docs" / "contracts" / "tools" / "build_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answer_for(body):
    """The fixture's predictions, echoing the request's data_version as the service does."""
    response = copy.deepcopy(RESPONSE)
    for prediction in response["predictions"]:
        prediction["data_version"] = body["data_version"]
    return response


class FakeModelService:
    """A local stand-in for brt-model (loopback is allowed in tests). It answers POST /predict from a
    script of (status, body or function of the request) and records every request."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __enter__(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                service.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
                status, answer = service.script.pop(0) if service.script else (500, {"detail": "script ran out"})
                payload = json.dumps(answer(body) if callable(answer) else answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class Waits(list):
    def __call__(self, seconds):
        self.append(seconds)


def post_with(service, waits):
    """post_predict against the fake service, with a fixed token and no real sleeping."""
    def post(url, body, config):
        return fs.post_predict(service.url, body, Settings(service.url, 5, config.retry_waits),
                               token=lambda audience: f"token-for-{audience}", sleep=waits)
    return post


# ---- the contract's fixtures ----

def test_the_rows_file_gives_the_contract_request():
    assert fs.request_body(fs.read_rows(ROWS), REQUEST["data_version"]) == REQUEST
    fs.validate(REQUEST, "predict-request.schema.json", "request")


def test_the_forecast_table_is_the_contract_file():
    table = fs.forecast_table(REQUEST, RESPONSE, ISSUED)
    assert table.equals(pq.read_table(FORECAST), check_metadata=True)
    assert fs.forecast_schema().equals(build_fixtures().FORECAST_SCHEMA)


def test_issued_at_is_whole_seconds_in_utc():
    table = fs.forecast_table(REQUEST, RESPONSE, datetime(2026, 11, 25, 23, 41, 7, 987654, tzinfo=timezone.utc))
    assert table.column("issued_at_utc")[0].as_py() == ISSUED


def test_the_response_fixture_passes_the_checks():
    fs.check_response(REQUEST, RESPONSE)


# ---- score, end to end ----

@pytest.fixture
def repo(tmp_path):
    rows = lm.forecast_dir(AS_OF, tmp_path) / "features.parquet"
    rows.parent.mkdir(parents=True)
    rows.write_bytes(ROWS.read_bytes())
    return tmp_path


def manifest_for(rows_path):
    """A manifest whose forecast_rows entry names the rows file as it is."""
    md5 = lm.file_md5(rows_path)
    return lm.canonical({"as_of": AS_OF.isoformat(), "contract": lm.CONTRACT,
                         "files": {"forecast_rows": {"md5": md5, "size_bytes": rows_path.stat().st_size,
                                                     "content_path": f"gs://boston-resilience-twin-data/dvc/files/md5/{md5[:2]}/{md5[2:]}"}}})


@pytest.fixture
def bucket(repo):
    bucket = fake_bucket()
    bucket.fake.objects[lm.manifest_path(AS_OF)] = (manifest_for(lm.forecast_dir(AS_OF, repo) / "features.parquet"), "application/json")
    return bucket


def version_of(bucket):
    return lm.data_version(bucket.fake.objects[lm.manifest_path(AS_OF)][0])


def test_score_against_a_fake_service(repo, bucket):
    waits = Waits()
    with FakeModelService((200, answer_for)) as service:
        result = fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, waits), now=ISSUED)

    [sent] = service.requests
    assert sent["path"] == "/predict"
    assert sent["authorization"] == f"Bearer token-for-{service.url}"  # the ID token's audience is the service URL
    assert sent["body"] == {**REQUEST, "data_version": version_of(bucket)}  # the rows as they are, and the live version
    assert waits == []

    written = bucket.fake.objects["forecasts/2026-11-26.parquet"]
    local = lm.forecast_dir(AS_OF, repo) / "forecast.parquet"
    assert written == (local.read_bytes(), fs.PARQUET)
    table = pq.read_table(local)
    expected = fs.forecast_table(REQUEST, answer_for(sent["body"]), ISSUED)
    assert table.equals(expected, check_metadata=True)
    assert set(table.column("data_version").to_pylist()) == {version_of(bucket)}

    flagged = sorted((p for p in RESPONSE["predictions"] if p["prediction"]), key=lambda p: (-p["probability"], p["unit"]))
    assert result == {
        "forecast_date": "2026-11-26",
        "flagged": [p["unit"] for p in flagged],
        "units": 24,
        "model_version": "1",
        "model_run_id": RESPONSE["predictions"][0]["model_run_id"],
        "data_version": version_of(bucket),
        "forecast_uri": "gs://boston-resilience-twin-data/forecasts/2026-11-26.parquet",
    }


def test_a_rerun_replaces_the_forecast(repo, bucket):
    waits = Waits()
    with FakeModelService((200, answer_for), (200, answer_for)) as service:
        fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, waits), now=ISSUED)
        later = datetime(2026, 11, 26, 0, 5, 0, tzinfo=timezone.utc)
        fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, waits), now=later)
    table = pq.read_table(lm.forecast_dir(AS_OF, repo) / "forecast.parquet")
    assert table.column("issued_at_utc")[0].as_py() == later
    assert bucket.fake.writes.count("forecasts/2026-11-26.parquet") == 2


def test_a_503_is_retried_after_each_wait(repo, bucket):
    waits = Waits()
    with FakeModelService((503, {"detail": "no model loaded"}), (503, {"detail": "no model loaded"}), (200, answer_for)) as service:
        fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, waits), now=ISSUED)
    assert len(service.requests) == 3
    assert waits == [30, 60]


def test_no_answer_after_every_try(repo, bucket):
    waits = Waits()
    with FakeModelService(*[(503, {"detail": "no model loaded"})] * 5) as service:
        with pytest.raises(fs.ScoringError, match="after 5 tries; the last: HTTP 503"):
            fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, waits), now=ISSUED)
    assert waits == [30, 60, 120, 240]
    assert "forecasts/2026-11-26.parquet" not in bucket.fake.objects  # never a forecast without a response


def test_a_422_is_not_retried(repo, bucket):
    waits = Waits()
    with FakeModelService((422, {"detail": [{"loc": ["body", "rows", 0, "wx_tmax_c"], "msg": "none is not allowed"}]})) as service:
        with pytest.raises(fs.ScoringError, match="rejected the request .HTTP 422., so it is not retried"):
            fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, waits), now=ISSUED)
    assert len(service.requests) == 1 and waits == []


def test_a_403_names_the_invoker_role(repo, bucket):
    with FakeModelService((403, {"detail": "Forbidden"})) as service:
        with pytest.raises(fs.ScoringError, match="needs roles/run.invoker"):
            fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, Waits()), now=ISSUED)


class FlakySession:
    """A session that fails before it answers: each item is an exception to raise or a (status, body)."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def post(self, url, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        response = requests.Response()
        response.status_code = status
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return response


def test_a_lost_connection_and_a_timeout_are_retried():
    session = FlakySession(requests.ConnectionError("connection reset"), requests.ReadTimeout("read timed out"),
                           (200, {"contract": "predict/v1", "predictions": []}))
    waits = Waits()
    answer = fs.post_predict("https://brt-model.example", {"rows": []}, FAST, token=lambda a: "t", session=session, sleep=waits)
    assert answer == {"contract": "predict/v1", "predictions": []}
    assert session.calls == 3 and waits == [30, 60]


def test_a_200_that_is_not_json_fails():
    session = FlakySession((200, b"<html>not json</html>"))
    with pytest.raises(fs.ScoringError, match="not JSON"):
        fs.post_predict("https://brt-model.example", {}, FAST, token=lambda a: "t", session=session, sleep=Waits())


# ---- what stops scoring before a request ----

def rewrite_rows(repo, change):
    """Rewrite the rows file through change(rows) and rewrite the manifest to match it."""
    path = lm.forecast_dir(AS_OF, repo) / "features.parquet"
    table = pq.read_table(path)
    rows = change(table.to_pylist())
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    return path


def test_more_than_half_missing_stops_and_names_the_columns(repo):
    def drop(rows):
        for i, row in enumerate(rows):
            row["snow_ice_rel7"] = None if i < 13 else row["snow_ice_rel7"]
            row["tree_rel3"] = None if i < 20 else row["tree_rel3"]
        return rows

    path = rewrite_rows(repo, drop)
    bucket = fake_bucket()
    bucket.fake.objects[lm.manifest_path(AS_OF)] = (manifest_for(path), "application/json")
    with FakeModelService() as service:
        with pytest.raises(fs.MissingInputs, match=r"no value for snow_ice_rel7 \(13 of 24\), tree_rel3 \(20 of 24\)"):
            fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, Waits()), now=ISSUED)
    assert service.requests == []


def test_exactly_half_missing_is_scored(repo):
    def drop(rows):
        for i, row in enumerate(rows):
            row["snow_ice_rel7"] = None if i < 12 else row["snow_ice_rel7"]
        return rows

    path = rewrite_rows(repo, drop)
    assert fs.mostly_missing(fs.read_rows(path)) == []


def test_a_null_the_contract_forbids_is_caught_before_sending(repo):
    def drop(rows):
        rows[3]["wx_tmax_c"] = None
        return rows

    path = rewrite_rows(repo, drop)
    bucket = fake_bucket()
    bucket.fake.objects[lm.manifest_path(AS_OF)] = (manifest_for(path), "application/json")
    with FakeModelService() as service:
        with pytest.raises(fs.ScoringError, match="the request breaks its contract .predict-request.schema.json.: rows/3/wx_tmax_c"):
            fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=post_with(service, Waits()), now=ISSUED)
    assert service.requests == []


@pytest.mark.parametrize("change, message", [
    (lambda rows: rows[:23], "23 forecast rows, not 24"),
    (lambda rows: rows[:23] + [dict(rows[0])], "units repeated"),
    (lambda rows: [{**row, "as_of": date(2026, 11, 24)} for row in rows], "not 2026-11-25 and 2026-11-26"),
])
def test_rows_that_cannot_be_a_forecast_fail(repo, change, message):
    rewrite_rows(repo, change)
    with pytest.raises(fs.ScoringError, match=message):
        fs.score(AS_OF, repo=repo, bucket=fake_bucket(), config=FAST, post=lambda *a: pytest.fail("sent"), now=ISSUED)


def test_no_rows_no_scoring(tmp_path):
    with pytest.raises(fs.ScoringError, match="forecast_rows .#160. writes them"):
        fs.score(AS_OF, repo=tmp_path, bucket=fake_bucket(), config=FAST, post=lambda *a: pytest.fail("sent"))


def test_no_manifest_no_scoring(repo):
    with pytest.raises(fs.ScoringError, match="the manifest task runs before scoring"):
        fs.score(AS_OF, repo=repo, bucket=fake_bucket(), config=FAST, post=lambda *a: pytest.fail("sent"))


def test_rows_changed_since_the_manifest(repo, bucket):
    rewrite_rows(repo, lambda rows: [{**row, "wx_precip_mm": 1.0} for row in rows])
    with pytest.raises(fs.ScoringError, match="changed since the manifest named it"):
        fs.score(AS_OF, repo=repo, bucket=bucket, config=FAST, post=lambda *a: pytest.fail("sent"))


def test_a_blank_predict_url_is_named(repo, bucket):
    with pytest.raises(fs.ScoringError, match="predict_url is blank in Data-Pipeline/config/evening_run.csv"):
        fs.score(AS_OF, repo=repo, bucket=bucket, config=Settings("", 5, ()), post=lambda *a: pytest.fail("sent"))


# ---- responses that break the contract ----

def broken(change):
    response = copy.deepcopy(RESPONSE)
    change(response)
    return response


@pytest.mark.parametrize("change, message", [
    (lambda r: r["predictions"].pop(), "23 predictions for 24 rows"),
    (lambda r: r["predictions"].reverse(), "prediction 0 is for West Roxbury"),
    (lambda r: r["predictions"][2].update(data_version="sha256:" + "0" * 64), "prediction 2 carries data_version"),
    (lambda r: r["predictions"][5].update(prediction=1 - r["predictions"][5]["prediction"]), "prediction 5"),
    (lambda r: r["predictions"][7].update(model_version="2"), "more than one model answered"),
    (lambda r: r["predictions"][0].update(model_run_id="ci-lightgbm"), "the response breaks its contract"),
])
def test_a_response_that_breaks_the_contract_fails(change, message):
    with pytest.raises(fs.ScoringError, match=message):
        fs.check_response(REQUEST, broken(change))


# ---- the 20:00 check, the ID token, and a light import ----

def test_check_forecast():
    bucket = fake_bucket()
    with pytest.raises(fs.ForecastMissing, match="forecasts/2026-11-26.parquet is not in the bucket at the 20:00 check"):
        fs.check_forecast(date(2026, 11, 26), bucket=bucket)
    bucket.write("forecasts/2026-11-26.parquet", b"x", fs.PARQUET)
    assert fs.check_forecast(date(2026, 11, 26), bucket=bucket) == "gs://boston-resilience-twin-data/forecasts/2026-11-26.parquet"


def test_an_id_token_failure_is_a_scoring_error(monkeypatch):
    import google.oauth2.id_token
    from google.auth.exceptions import DefaultCredentialsError

    def no_token(request, audience):
        raise DefaultCredentialsError("Neither metadata server or valid service account credentials are found.")

    monkeypatch.setattr(google.oauth2.id_token, "fetch_id_token", no_token)
    with pytest.raises(fs.ScoringError, match="could not get an ID token for https://brt-model.example"):
        fs.id_token("https://brt-model.example")


def test_the_evening_modules_import_light():
    # The DAG imports them at parse time.
    code = ("import sys, evening_run, data_bucket, live_manifest, forecast_scoring; "
            "print(sorted({'pandas', 'numpy', 'pyarrow', 'jsonschema', 'requests', 'google.cloud.storage'} & set(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(fs.__file__).parent, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
