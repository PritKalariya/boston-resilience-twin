import json
import logging
import math

import numpy as np
import pandas as pd
import pytest

from conftest import HERE, MLFLOW_RUN_ID, MODEL_VERSION, THRESHOLD, copy_model
from test_health import set_threshold


def post(client, body):
    return client.post("/predict", json=body)


def reference_frame(rows, contract, features):
    """The request rows as a frame, built here and not by the service."""
    frame = pd.DataFrame(rows)
    for name in features:
        frame[name] = frame[name].astype(contract.dtypes[name])
    return frame[list(features)]


def test_predict_on_the_d5_request(served, body, contract, wrapper):
    response = post(served, body)
    assert response.status_code == 200
    answer = response.json()
    assert list(contract.response_validator.iter_errors(answer)) == []
    assert [p["unit"] for p in answer["predictions"]] == [r["unit"] for r in body["rows"]]
    for prediction in answer["predictions"]:
        assert prediction["data_version"] == body["data_version"]
        assert prediction["model_run_id"] == MLFLOW_RUN_ID
        assert prediction["model_version"] == MODEL_VERSION
        assert prediction["threshold"] == THRESHOLD
        assert prediction["date"] == "2026-11-26"
        assert prediction["prediction"] == int(prediction["probability"] >= THRESHOLD)
    expected = wrapper.predict_proba(reference_frame(body["rows"], contract, wrapper.inputs))
    assert [p["probability"] for p in answer["predictions"]] == expected.tolist()  # exactly, not rounded


def test_a_probability_equal_to_the_threshold_is_a_surge(make_client, model_dir, body, tmp_path):
    first = post(make_client("model", model_dir=model_dir), body).json()["predictions"][0]["probability"]
    folder = copy_model(model_dir, tmp_path / "at-threshold")
    set_threshold(folder, first)
    answer = post(make_client("model", model_dir=folder), body).json()
    assert answer["predictions"][0]["threshold"] == first
    assert answer["predictions"][0]["prediction"] == 1


def test_inputs_that_may_be_null_are_scored(served, body, contract, wrapper):
    body["rows"][0]["snow_ice_rel3"] = None
    body["rows"][1]["static_population"] = None
    answer = post(served, body)
    assert answer.status_code == 200
    frame = reference_frame(body["rows"], contract, wrapper.inputs)
    assert [p["probability"] for p in answer.json()["predictions"]] == wrapper.predict_proba(frame).tolist()
    assert np.isnan(frame.loc[0, "snow_ice_rel3"])


NULLS = HERE / "data" / "request_nulls.json"


def test_the_nulls_fixture_nulls_every_nullable_input_and_changes_nothing_else(contract, request_body):
    nulls = json.loads(NULLS.read_text(encoding="utf-8"))
    properties = contract.request_validator.schema["$defs"]["row"]["properties"]
    nullable = {n for n, v in properties.items() if "null" in v.get("type", [])}
    assert len(nullable) == 14
    for name in nullable:
        assert any(row[name] is None for row in nulls["rows"]), name
    for new, old in zip(nulls["rows"], request_body["rows"], strict=True):
        assert {k: v for k, v in new.items() if k not in nullable} == {k: v for k, v in old.items() if k not in nullable}
        assert all(new[k] is None or new[k] == old[k] for k in nullable)
    assert {k: v for k, v in nulls.items() if k != "rows"} == {k: v for k, v in request_body.items() if k != "rows"}


def test_a_request_with_every_nullable_input_missing_somewhere_is_scored(served, contract):
    response = post(served, json.loads(NULLS.read_text(encoding="utf-8")))
    assert response.status_code == 200
    assert list(contract.response_validator.iter_errors(response.json())) == []
    probabilities = [p["probability"] for p in response.json()["predictions"]]
    assert len(probabilities) == 24 and all(math.isfinite(p) for p in probabilities)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_constant_that_is_not_json_is_422_and_never_reaches_the_model(served, request_body, constant):
    # json.dumps writes the constants as bare words; swap one value for the bare word
    text = json.dumps(request_body).replace('"wx_precip_mm": 4.2', f'"wx_precip_mm": {constant}', 1)
    assert constant in text
    response = served.post("/predict", content=text.encode(), headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"detail": [{"path": "", "message": f"{constant} is not allowed: it is not a JSON value"}]}


def test_one_row_is_enough(served, body):
    body["rows"] = body["rows"][:1]
    assert len(post(served, body).json()["predictions"]) == 1


def drop(row, name):
    del row[name]


def add(row, name):
    row[name] = 1


def put(value):
    return lambda row, name: row.__setitem__(name, value)


@pytest.mark.parametrize("mutate, name, where", [
    (drop, "month", "/rows/0"),
    (add, "unexpected_input", "/rows/0"),
    (put("wet"), "wx_precip_mm", "/rows/0/wx_precip_mm"),
    (put(None), "wx_tmax_c", "/rows/0/wx_tmax_c"),  # a null where none is allowed
    (put(13), "month", "/rows/0/month"),
    (put("2026-11-31"), "as_of", "/rows/0/as_of"),  # not a calendar day
])
def test_a_row_that_breaks_the_schema_is_422(served, body, mutate, name, where):
    mutate(body["rows"][0], name)
    response = post(served, body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail and all(set(item) == {"path", "message"} for item in detail)
    assert any(item["path"].startswith(where) for item in detail)


def test_a_wrong_type_names_the_field(served, body):
    body["rows"][3]["day_of_year"] = "330"
    assert [e["path"] for e in post(served, body).json()["detail"]] == ["/rows/3/day_of_year"]


def test_the_request_envelope_is_checked(served, body):
    assert post(served, {**body, "contract": "predict/v2"}).status_code == 422
    assert post(served, {**body, "data_version": "sha256:abc"}).status_code == 422
    assert post(served, {**body, "rows": []}).status_code == 422
    assert post(served, {**body, "rows": body["rows"] * 2}).status_code == 422  # more than 24
    assert post(served, [body]).status_code == 422
    not_json = served.post("/predict", content=b"{", headers={"content-type": "application/json"})
    assert not_json.status_code == 422 and not_json.json()["detail"][0]["message"] == "the body is not valid JSON"


def test_duplicate_units_are_422(served, body):
    body["rows"][1]["unit"] = body["rows"][0]["unit"]
    detail = post(served, body).json()["detail"]
    assert post(served, body).status_code == 422
    assert detail[0]["path"] == "/rows/1/unit" and "repeated" in detail[0]["message"]


def test_rows_with_different_dates_are_422(served, body):
    body["rows"][1]["forecast_date"] = "2026-11-27"
    body["rows"][2]["as_of"] = "2026-11-24"
    response = post(served, body)
    assert response.status_code == 422
    messages = " ".join(e["message"] for e in response.json()["detail"])
    assert "same forecast_date" in messages and "same as_of" in messages


@pytest.mark.parametrize("forecast_date", ["2026-11-25", "2026-11-27", "2026-12-26"])
def test_forecast_date_must_be_as_of_plus_one_day(served, body, forecast_date):
    for row in body["rows"]:
        row["forecast_date"] = forecast_date
    response = post(served, body)
    assert response.status_code == 422
    assert "plus one day" in response.json()["detail"][0]["message"]


def test_inputs_must_be_the_models_features(make_client, subset_model_dir, body):
    """A model that takes 30 inputs: the contract's 31st is an extra column, so the request is refused."""
    client = make_client("model", model_dir=subset_model_dir)
    response = post(client, body)
    assert response.status_code == 422
    assert {e["message"] for e in response.json()["detail"]} == {"the served model does not take input 'tide_pred_max_m'"}
    assert len(response.json()["detail"]) == len(body["rows"])  # one per row
    for row in body["rows"]:
        del row["tide_pred_max_m"]
    assert post(client, body).status_code == 422  # now the schema says the contract's input is missing


def test_predict_is_503_with_no_model(make_client, body, tmp_path):
    response = post(make_client("model", model_dir=tmp_path / "empty"), body)
    assert response.status_code == 503


def test_every_request_logs_one_json_line_without_inputs(served, body):
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Capture()
    log = logging.getLogger("serving")
    log.addHandler(handler)
    try:
        post(served, body)
        body["rows"][0]["month"] = 99
        post(served, body)
    finally:
        log.removeHandler(handler)
    records = [json.loads(line) for line in lines]
    assert [r["status"] for r in records] == [200, 422]
    assert [r["rows"] for r in records] == [24, 0]
    for record in records:
        assert set(record) == {"event", "status", "rows", "model_version", "latency_ms"}
        assert record["model_version"] == MODEL_VERSION and record["latency_ms"] >= 0
    assert "Allston" not in "".join(lines) and "wx_precip_mm" not in "".join(lines)
