"""Serving/scripts/parity.py (#161): the check that the running service scores like the training code, to 1e-9.

No network and no real model: `urllib.request.urlopen` and `joblib.load` are faked, so each test is about one
decision of the script (the tolerance, the unit check, the status check, the token, the Arrow cast). The exit
codes are read from a real child process too, because `sys.exit("FAIL: ...")` is status 1 only there."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
import urllib.error
from pathlib import Path

import joblib
import numpy as np
import parity
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

TOKEN = "eyJhbGciOi.SECRET-IDENTITY-TOKEN.0123456789"
UNITS = ["Allston", "Back Bay", "Dorchester"]
SCRIPTS = Path(parity.__file__).resolve().parent


def request_of(units=UNITS) -> dict:
    return {"rows": [{"unit": unit, "forecast_date": "2026-11-26", "as_of": "2026-11-25"} for unit in units]}


class Answer(io.BytesIO):
    """What urlopen returns: a body that is a context manager, with a status."""

    def __init__(self, body: dict | bytes, status: int = 200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        self.status = status


def predictions(pairs) -> dict:
    return {"predictions": [{"unit": unit, "probability": probability} for unit, probability in pairs]}


@pytest.fixture
def urlopen(monkeypatch):
    """Fake urlopen: set .answer (or .raises) and read what was sent from .sent."""

    class Fake:
        answer = None
        raises = None
        sent: list = []

        def __call__(self, request, timeout=None):
            self.sent.append(request)
            if self.raises:
                raise self.raises
            return self.answer

    fake = Fake()
    fake.sent = []
    monkeypatch.setattr(parity.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def run_main(monkeypatch, tmp_path, urlopen, capsys):
    """main() with the training-way probabilities `want` and a service that answers `got`, both unit -> p."""

    def run(want: dict, got, extra=()):
        monkeypatch.setattr(parity, "reference", lambda request, model_dir, features: want)
        urlopen.answer = Answer(predictions(got.items() if isinstance(got, dict) else got))
        path = tmp_path / "request.json"
        path.write_text(json.dumps(request_of(list(want))), encoding="utf-8")
        code = parity.main(["--request", str(path), "--model-dir", "unused", "--url", "http://svc.test/", *extra])
        return code, capsys.readouterr()

    return run


def exit_status(call) -> int:
    """The process status of a call that ends in sys.exit: Python gives a message string status 1."""
    with pytest.raises(SystemExit) as raised:
        call()
    code = raised.value.code
    return code if isinstance(code, int) else 1


# ---- the tolerance


def test_a_difference_of_zero_passes(run_main):
    want = {unit: 0.25 for unit in UNITS}
    code, out = run_main(want, dict(want))
    assert code == 0 and "PASS" in out.out and "FAIL" not in out.out


def test_a_difference_of_exactly_1e_9_passes(run_main):
    code, out = run_main({"Allston": 0.0}, {"Allston": 1e-9})
    assert abs(0.0 - 1e-9) == parity.TOLERANCE == 1e-9
    assert code == 0 and "PASS" in out.out


def test_a_difference_of_2e_9_fails(run_main):
    code, out = run_main({"Allston": 0.0}, {"Allston": 2e-9})
    assert code == 1 and "FAIL" in out.out and "max absolute difference 2e-09" in out.out


def test_the_worst_unit_decides(run_main):
    want = {"Allston": 0.1, "Back Bay": 0.2, "Dorchester": 0.3}
    code, out = run_main(want, {"Allston": 0.1, "Back Bay": 0.2, "Dorchester": 0.3 + 1e-6})
    assert code == 1 and "FAIL" in out.out


def test_a_nan_probability_fails(run_main):
    code, out = run_main({"Allston": 0.5}, {"Allston": float("nan")})
    assert code == 1 and "FAIL" in out.out


# ---- the units in the response


def test_a_unit_missing_from_the_response_fails(urlopen):
    urlopen.answer = Answer(predictions([("Allston", 0.5), ("Back Bay", 0.5)]))
    with pytest.raises(SystemExit) as raised:
        parity.served(request_of(), "http://svc.test", None)
    assert str(raised.value.code).startswith("FAIL") and "Dorchester" in str(raised.value.code)
    urlopen.answer = Answer(predictions([("Allston", 0.5), ("Back Bay", 0.5)]))  # a closed body cannot be read twice
    assert exit_status(lambda: parity.served(request_of(), "http://svc.test", None)) == 1


def test_an_extra_unit_in_the_response_fails(urlopen):
    urlopen.answer = Answer(predictions([(unit, 0.5) for unit in [*UNITS, "Roxbury"]]))
    with pytest.raises(SystemExit) as raised:
        parity.served(request_of(), "http://svc.test", None)
    assert str(raised.value.code).startswith("FAIL") and "Roxbury" in str(raised.value.code)


def test_units_in_another_order_fail(urlopen):
    urlopen.answer = Answer(predictions([(unit, 0.5) for unit in reversed(UNITS)]))
    assert exit_status(lambda: parity.served(request_of(), "http://svc.test", None)) == 1


def test_the_units_in_request_order_are_returned_by_unit(urlopen):
    urlopen.answer = Answer(predictions([("Allston", 0.1), ("Back Bay", 0.2), ("Dorchester", 0.3)]))
    assert parity.served(request_of(), "http://svc.test", None) == {"Allston": 0.1, "Back Bay": 0.2, "Dorchester": 0.3}


# ---- a non-200 answer


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
def test_an_error_status_fails_and_names_it(urlopen, status):
    urlopen.raises = urllib.error.HTTPError("http://svc.test/predict", status, "no", {}, io.BytesIO(b"the body"))
    with pytest.raises(SystemExit) as raised:
        parity.served(request_of(), "http://svc.test", None)
    assert str(raised.value.code).startswith("FAIL") and f"answered {status}" in str(raised.value.code)
    assert exit_status(lambda: parity.served(request_of(), "http://svc.test", None)) == 1


@pytest.mark.parametrize("status", [201, 204, 302])
def test_a_success_status_other_than_200_fails(urlopen, status):
    urlopen.answer = Answer(predictions([(unit, 0.5) for unit in UNITS]), status=status)
    with pytest.raises(SystemExit) as raised:
        parity.served(request_of(), "http://svc.test", None)
    assert f"answered {status}" in str(raised.value.code)


def test_a_service_that_cannot_be_reached_fails(urlopen):
    urlopen.raises = urllib.error.URLError("connection refused")
    with pytest.raises(SystemExit) as raised:
        parity.served(request_of(), "http://svc.test", None)
    assert "cannot reach http://svc.test" in str(raised.value.code)


# ---- the token


def test_the_token_is_sent_as_a_bearer_header(urlopen):
    urlopen.answer = Answer(predictions([(unit, 0.5) for unit in UNITS]))
    parity.served(request_of(), "http://svc.test/", TOKEN)
    sent = urlopen.sent[0]
    assert sent.get_header("Authorization") == f"Bearer {TOKEN}"
    assert sent.full_url == "http://svc.test/predict" and sent.get_method() == "POST"
    assert sent.get_header("Content-type") == "application/json"
    assert json.loads(sent.data) == request_of()


def test_no_token_means_no_authorization_header(urlopen):
    urlopen.answer = Answer(predictions([(unit, 0.5) for unit in UNITS]))
    parity.served(request_of(), "http://svc.test", None)
    assert urlopen.sent[0].get_header("Authorization") is None


def test_the_token_is_never_printed(run_main, urlopen, capsys):
    want = {unit: 0.25 for unit in UNITS}
    code, out = run_main(want, dict(want), extra=["--token", TOKEN])
    assert code == 0 and urlopen.sent[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in out.out and TOKEN not in out.err
    # nor in a failure
    urlopen.raises = urllib.error.HTTPError("http://svc.test/predict", 401, "no", {}, io.BytesIO(b"unauthorized"))
    with pytest.raises(SystemExit) as raised:
        parity.served(request_of(), "http://svc.test", TOKEN)
    assert TOKEN not in str(raised.value.code)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err


# ---- the cast to the Arrow types of the training features file


class RecordingModel:
    """Stands in for the saved model: records the frame it is given and returns a fixed probability per row."""

    feature_names_in_ = np.array(["count", "ratio"])

    def __init__(self):
        self.frames = []

    def predict_proba(self, frame):
        self.frames.append(frame)
        p = np.linspace(0.1, 0.9, len(frame))
        return np.column_stack([1 - p, p])


def test_the_request_rows_are_cast_to_the_arrow_types_of_the_features_file(tmp_path, monkeypatch):
    features = tmp_path / "features.parquet"
    pq.write_table(pa.table({"count": pa.array([1], pa.int32()), "ratio": pa.array([0.5], pa.float32()),
                             "not_used": pa.array(["x"])}), features)
    model, loaded = RecordingModel(), []
    monkeypatch.setattr(joblib, "load", lambda path: loaded.append(path) or model)
    request = {"rows": [{"unit": "Allston", "ratio": 0.1, "count": 3, "extra": "ignored"},
                        {"unit": "Back Bay", "ratio": 0.7, "count": 4, "extra": "ignored"}]}

    result = parity.reference(request, str(tmp_path / "model"), str(features))

    assert loaded == [f"{tmp_path / 'model'}/model.joblib"]
    frame = model.frames[0]
    assert list(frame.columns) == ["count", "ratio"]  # the model's order, not the request's
    assert str(frame["count"].dtype) == "int32" and str(frame["ratio"].dtype) == "float32"
    assert frame["ratio"].tolist() == [float(np.float32(0.1)), float(np.float32(0.7))]  # float32 values, not 0.1
    assert result == {"Allston": pytest.approx(0.1), "Back Bay": pytest.approx(0.9)}
    assert all(isinstance(value, float) for value in result.values())


def test_a_value_the_arrow_type_cannot_hold_is_an_error_not_a_silent_change(tmp_path, monkeypatch):
    features = tmp_path / "features.parquet"
    pq.write_table(pa.table({"count": pa.array([1], pa.int32()), "ratio": pa.array([0.5], pa.float32())}), features)
    monkeypatch.setattr(joblib, "load", lambda path: RecordingModel())
    request = {"rows": [{"unit": "Allston", "ratio": 0.1, "count": 2.5}]}
    with pytest.raises(pa.ArrowInvalid):
        parity.reference(request, str(tmp_path), str(features))


# ---- the exit codes of the script as a process


DRIVER = textwrap.dedent("""
    import io, json, sys, urllib.error
    sys.path.insert(0, {scripts!r})
    import parity

    scenario, request = sys.argv[1], sys.argv[2]
    parity.reference = lambda request, model_dir, features: {{"Allston": 0.0}}

    class Answer(io.BytesIO):
        status = 200

    def answering(p):
        return lambda post, timeout=None: Answer(json.dumps({{"predictions": [{{"unit": "Allston", "probability": p}}]}}).encode())

    def refusing(post, timeout=None):
        raise urllib.error.HTTPError(post.full_url, 503, "no", {{}}, io.BytesIO(b"down"))

    parity.urllib.request.urlopen = {{"pass": answering(1e-9), "fail": answering(2e-9), "http": refusing}}[scenario]
    sys.exit(parity.main(["--request", request, "--model-dir", "m", "--url", "http://svc.test"]))
""")


@pytest.mark.parametrize("scenario, status, where, text", [
    ("pass", 0, "out", "PASS"),
    ("fail", 1, "out", "FAIL"),
    ("http", 1, "err", "FAIL: /predict answered 503"),
])
def test_the_exit_status_of_the_script(tmp_path, scenario, status, where, text):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.format(scripts=str(SCRIPTS)), encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_of(["Allston"])), encoding="utf-8")
    done = subprocess.run([sys.executable, "-I", str(driver), scenario, str(request)],
                          capture_output=True, text=True, check=False, timeout=60)
    assert done.returncode == status, done.stderr
    assert text in (done.stdout if where == "out" else done.stderr)
