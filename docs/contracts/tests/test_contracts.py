"""The deployment contracts (#159): every schema is valid draft 2020-12, every fixture matches its schema,
the cross-field rules hold, and a mutated copy of each fixture fails (so the checks are not vacuous).

Run as CI runs it: docker compose run --rm test docs/contracts/tests --no-cov
Uses only jsonschema and pyarrow, which the Airflow image already has.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator, FormatChecker

CONTRACTS = Path(__file__).resolve().parents[1]
ROOT = CONTRACTS.parents[1]
SCHEMAS = CONTRACTS / "schemas"
FIXTURES = CONTRACTS / "fixtures"
CATALOG = ROOT / "Data-Pipeline" / "config" / "feature_catalog.csv"
POVERTY_GROUPS = ROOT / "Model-Development" / "config" / "poverty_groups.csv"
DVC_CONFIG = ROOT / ".dvc" / "config"
BUILD_FIXTURES = CONTRACTS / "tools" / "build_fixtures.py"
TRAINING_CSV = ROOT / "Model-Development" / "config" / "training.csv"

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_NAMES = ["predict-request", "predict-response", "forecast-row", "data-manifest", "monitoring-output",
                "retrain-decision", "agent-brief", "metric-names"]
PAGES = {
    "predict.md": (["predict-request", "predict-response"], ["predict/request.json", "predict/response.json",
                                                         "forecast-rows/2026-11-25/features.parquet"]),
    "forecast-file.md": (["forecast-row"], ["forecasts/2026-11-26.parquet", "forecasts/2026-11-26.records.json"]),
    "data-manifest.md": (["data-manifest"], ["manifests/2026-11-25.json", "manifests/latest.json",
                                             "manifests/data_version.txt"]),
    "monitoring-output.md": (["monitoring-output"], ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"]),
    "retrain-decision.md": (["retrain-decision"], ["monitoring/decisions/2026-11-27.json",
                                                   "monitoring/decisions/2026-12-03.json",
                                                   "monitoring/decisions/latest.json"]),
    "agent-brief.md": (["agent-brief"], ["forecasts/2026-11-26.brief.json"]),
    "metric-names.md": (["metric-names"], ["metrics/metric-names.json"]),
}
SECTIONS = ["Purpose", "Location", "Producer", "Consumers", "Fields", "Rules", "Example", "Schema", "Versioning",
            "Where it breaks"]
# fixture (relative to fixtures/) -> schema name; the forecast records are an array of forecast-row
JSON_FIXTURES = {
    "predict/request.json": "predict-request",
    "predict/response.json": "predict-response",
    "manifests/2026-11-25.json": "data-manifest",
    "manifests/latest.json": "data-manifest",
    "monitoring/2026-11-27.json": "monitoring-output",
    "monitoring/2026-12-03.json": "monitoring-output",
    "monitoring/decisions/2026-11-27.json": "retrain-decision",
    "monitoring/decisions/2026-12-03.json": "retrain-decision",
    "monitoring/decisions/latest.json": "retrain-decision",
    "forecasts/2026-11-26.brief.json": "agent-brief",
    "metrics/metric-names.json": "metric-names",
}
RECORDS = "forecasts/2026-11-26.records.json"
PARQUET = "forecasts/2026-11-26.parquet"
ROWS_PARQUET = "forecast-rows/2026-11-25/features.parquet"
ROWS_INT_COLUMNS = {"day_of_week", "month", "day_of_year", "alert_snow_ice_count", "alert_snow_ice_warning",
                    "alert_tree_count", "alert_tree_warning", "alert_flooding_count", "alert_flooding_warning"}
# the inputs that may be null (predict.md, "How null was decided"): the 8 relative 311 columns and the 6 census ones
NULLABLE_INPUTS = {"snow_ice_rel_last", "snow_ice_rel3", "snow_ice_rel7", "snow_ice_rel28", "tree_rel_last",
                   "tree_rel3", "tree_rel7", "tree_rel28", "static_population", "static_land_sqmi", "static_density",
                   "static_poverty_share", "static_older_adult_share", "static_no_vehicle_share"}
NEVER_NULL_WEATHER = ["wx_precip_mm", "wx_snowfall_mm", "wx_tmax_c", "wx_tmin_c", "wx_gust_max_ms", "tide_pred_max_m"]
FORECAST_SCHEMA = pa.schema([
    ("unit", pa.string()), ("forecast_date", pa.date32()), ("as_of", pa.date32()), ("probability", pa.float64()),
    ("prediction", pa.int8()), ("threshold", pa.float64()), ("model_version", pa.string()),
    ("model_run_id", pa.string()), ("data_version", pa.string()), ("issued_at_utc", pa.timestamp("us", tz="UTC"))])
THRESHOLD = 0.096
NUMBER_WORDS = {8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}
REASONS = {"insufficient data", "no data", "undefined"}
MIN_POSITIVES = 5
RUN_ID = "3f9a1c07d2e84b56a0c1e7d94b2f6a85"
# schema -> where the unit enum and the input-column enum must be defined, once each
SCHEMAS_WITH_UNIT = ["predict-request", "predict-response", "forecast-row", "agent-brief"]
SCHEMAS_WITH_FEATURE = ["predict-request", "monitoring-output", "agent-brief"]


# ---------------------------------------------------------------- helpers
def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def load_fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name), format_checker=FormatChecker())


def errors(name: str, doc) -> list:
    return list(validator(name).iter_errors(doc))


def catalog_inputs() -> list[str]:
    with CATALOG.open(newline="", encoding="utf-8") as handle:
        return [r["feature"] for r in csv.DictReader(handle) if r["use_in_model"].strip() == "yes"]


def forecast_inputs() -> list[str]:
    """The `forecast_inputs` setting of training.csv, read now."""
    with TRAINING_CSV.open(newline="", encoding="utf-8") as handle:
        value = next(r["value"] for r in csv.DictReader(handle) if r["setting"] == "forecast_inputs")
    return [name.strip() for name in value.split(";") if name.strip()]


def rows_file() -> pa.Table:
    return pq.read_table(FIXTURES / ROWS_PARQUET)


def nullable_inputs(schema: dict) -> set[str]:
    """The inputs the request schema lets be null."""
    props = schema["$defs"]["row"]["properties"]
    return {name for name, node in props.items() if "null" in (node.get("type") or [])}


def poverty_units() -> list[str]:
    with POVERTY_GROUPS.open(newline="", encoding="utf-8") as handle:
        return [r["neighborhood"] for r in csv.DictReader(handle)]


def poverty_group_names() -> set[str]:
    with POVERTY_GROUPS.open(newline="", encoding="utf-8") as handle:
        return {r["group"] for r in csv.DictReader(handle)} - {"ungrouped"}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def no_floats(text: str) -> None:
    def refuse(token):
        raise AssertionError(f"float {token} in a manifest")
    json.loads(text, parse_float=refuse)


def walk(node, path=()):
    """Every (path, value) in a JSON document, containers included."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*path, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, (*path, i))


def metric_objects(doc):
    """Every object with exactly the keys value and reason."""
    return [node for _, node in walk(doc) if isinstance(node, dict) and set(node) == {"value", "reason"}]


def get_path(doc, dotted: str):
    node = doc
    for part in dotted.split("."):
        node = node[part]
    return node


def dvc_remote() -> tuple[str, str]:
    """(bucket, prefix) of the DVC remote, read from .dvc/config now."""
    url = re.search(r"url\s*=\s*gs://([^/\s]+)/(\S+)", DVC_CONFIG.read_text(encoding="utf-8"))
    assert url, "no gs:// remote in .dvc/config"
    return url.group(1), url.group(2)


def data_version() -> str:
    return "sha256:" + hashlib.sha256((FIXTURES / "manifests" / "latest.json").read_bytes()).hexdigest()


# ---------------------------------------------------------------- schemas
def test_the_eight_schemas_exist():
    assert sorted(p.name.removesuffix(".schema.json") for p in SCHEMAS.glob("*.schema.json")) == sorted(SCHEMA_NAMES)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft_2020_12(name):
    schema = load_schema(name)
    assert schema["$schema"] == DRAFT_2020_12
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_forbids_extra_fields_everywhere(name):
    """v1 rule: every object schema sets additionalProperties false."""
    for path, node in walk(load_schema(name)):
        if isinstance(node, dict) and node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, f"{name}: {'/'.join(map(str, path))}"


def _enum_nodes(schema):
    """(path, list) of every enum in a schema."""
    return [(path, node) for path, node in walk(schema) if path and path[-1] == "enum" and isinstance(node, list)]


@pytest.mark.parametrize("name", SCHEMAS_WITH_UNIT)
def test_unit_enum_is_defined_once_and_equals_poverty_groups(name):
    units = poverty_units()
    enum = load_schema(name)["$defs"]["unit"]["enum"]
    assert len(enum) == len(set(enum)) == len(units)
    assert sorted(enum) == sorted(units)


@pytest.mark.parametrize("name", SCHEMAS_WITH_FEATURE)
def test_feature_enum_is_defined_once_and_equals_the_catalog(name):
    inputs = catalog_inputs()
    enum = load_schema(name)["$defs"]["feature"]["enum"]
    assert len(enum) == len(set(enum)) == len(inputs)
    assert set(enum) == set(inputs)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_no_other_enum_holds_a_unit_or_a_catalog_column(name):
    """A second copy of either list would drift from the config files and from $defs, so there is none."""
    names = set(poverty_units()) | set(catalog_inputs())
    schema = load_schema(name)
    defined_here = {("$defs", "unit", "enum"), ("$defs", "feature", "enum")}
    for path, node in _enum_nodes(schema):
        if tuple(path) in defined_here:
            continue
        assert not names & set(map(str, node)), f"{name}: {'/'.join(map(str, path))} repeats a unit or a column"


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_every_unit_and_feature_use_is_a_ref(name):
    """The $defs are only worth having if the uses point at them: no inline enum, only $ref."""
    refs = {node["$ref"] for _, node in walk(load_schema(name)) if isinstance(node, dict) and "$ref" in node}
    assert not refs - {"#/$defs/" + key for key in load_schema(name)["$defs"]}
    for want in ("unit", "feature"):
        if want in load_schema(name)["$defs"]:
            assert f"#/$defs/{want}" in refs, f"{name}: $defs.{want} is never used"


@pytest.mark.parametrize("name", ["forecast-row", "data-manifest"])
def test_timestamp_fields_have_a_format_and_a_pattern(name):
    """format date-time is only checked when rfc3339-validator is installed (the Airflow image has none), so the
    pattern is the check that always runs."""
    schema = load_schema(name)
    fields = [k for k in schema["properties"] if k.endswith("_utc")]
    assert fields
    for field in fields:
        ref = schema["properties"][field]["$ref"].removeprefix("#/$defs/")
        node = schema["$defs"][ref]
        assert node["format"] == "date-time"
        assert node["pattern"] == r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


# ---------------------------------------------------------------- fixtures validate
@pytest.mark.parametrize("rel,name", sorted(JSON_FIXTURES.items()))
def test_fixture_validates(rel, name):
    assert errors(name, load_fixture(rel)) == []


def test_forecast_records_validate():
    records = load_fixture(RECORDS)
    assert isinstance(records, list) and len(records) == 24
    for row in records:
        assert errors("forecast-row", row) == []


@pytest.mark.parametrize("rel,name", sorted(JSON_FIXTURES.items()))
def test_contract_field_matches_the_schema(rel, name):
    doc = load_fixture(rel)
    assert doc["contract"] == load_schema(name)["properties"]["contract"]["const"]
    assert re.fullmatch(r"[a-z-]+/v1", doc["contract"])


# ---------------------------------------------------------------- mutations: the checks are not vacuous
def _drop_first_required(doc, schema):
    bad = copy.deepcopy(doc)
    del bad[schema["required"][0]]
    return bad


def _drop_row_field(doc, key):
    bad = copy.deepcopy(doc)
    first = bad[key][0]
    del first[next(k for k in first if k not in ("unit", "forecast_date", "as_of", "date"))]
    return bad


@pytest.mark.parametrize("rel,name", sorted(JSON_FIXTURES.items()))
def test_dropped_required_field_fails(rel, name):
    schema = load_schema(name)
    assert errors(name, _drop_first_required(load_fixture(rel), schema)), "a dropped required field passed"


@pytest.mark.parametrize("rel,name", sorted(JSON_FIXTURES.items()))
def test_extra_top_level_field_fails(rel, name):
    bad = load_fixture(rel)
    bad["unexpected"] = 1
    assert errors(name, bad), "an extra field passed"


def test_dropped_and_extra_nested_fields_fail():
    for rel, name, key in [("predict/request.json", "predict-request", "rows"),
                           ("predict/response.json", "predict-response", "predictions")]:
        assert errors(name, _drop_row_field(load_fixture(rel), key)), f"{rel}: a dropped row field passed"
        extra = load_fixture(rel)
        extra[key][0]["unexpected"] = 1
        assert errors(name, extra), f"{rel}: an extra row field passed"
    row = load_fixture(RECORDS)[0]
    del row["threshold"]
    assert errors("forecast-row", row)
    row = load_fixture(RECORDS)[0]
    row["unexpected"] = 1
    assert errors("forecast-row", row)


def test_a_float_in_the_manifest_fails():
    bad = load_fixture("manifests/latest.json")
    bad["files"]["labels"]["size_bytes"] = 1.5
    assert errors("data-manifest", bad)
    with pytest.raises(AssertionError, match="float"):
        no_floats(json.dumps(bad))
    # a float written as 1.0 passes the integer type in JSON Schema, so the text check is the one that catches it
    with pytest.raises(AssertionError, match="float"):
        no_floats('{"size_bytes": 1.0}')


@pytest.mark.parametrize("rel,name,where", [
    ("predict/request.json", "predict-request", ("rows", 0, "unit")),
    ("predict/response.json", "predict-response", ("predictions", 0, "unit")),
    ("forecasts/2026-11-26.brief.json", "agent-brief", ("flagged", 0, "unit")),
    ("forecasts/2026-11-26.brief.json", "agent-brief", ("claims", 1, "source", "row", "unit")),
])
def test_unknown_unit_fails(rel, name, where):
    bad = load_fixture(rel)
    node = bad
    for part in where[:-1]:
        node = node[part]
    node[where[-1]] = "Atlantis"
    assert errors(name, bad), "an unknown unit passed"


def test_unknown_unit_in_a_forecast_row_fails():
    row = load_fixture(RECORDS)[0]
    row["unit"] = "Atlantis"
    assert errors("forecast-row", row)


def test_unknown_input_value_fails():
    bad = load_fixture("predict/request.json")
    bad["rows"][0]["month"] = 13
    assert errors("predict-request", bad)
    bad = load_fixture("predict/request.json")
    bad["rows"][0]["alert_tree_warning"] = 2
    assert errors("predict-request", bad)
    bad = load_fixture("predict/request.json")
    bad["rows"][0]["day_of_week"] = None  # calendar columns are never null
    assert errors("predict-request", bad)


@pytest.mark.parametrize("name", NEVER_NULL_WEATHER)
def test_a_null_weather_or_tide_input_fails(name):
    """A day with no forecast is not scored in training and the evening rows stop before sending one."""
    bad = load_fixture("predict/request.json")
    bad["rows"][0][name] = None
    assert errors("predict-request", bad), f"a null {name} passed"


def test_a_null_association_value_fails():
    bad = load_fixture("forecasts/2026-11-26.brief.json")
    bad["flagged"][0]["associations"][0]["value"] = None
    assert errors("agent-brief", bad), "an association with a null value passed"


def test_the_forecast_inputs_are_never_nullable_in_the_request():
    """training.csv forecast_inputs: a validation or test row missing one is not scored, so a live null there was
    never scored either."""
    names = forecast_inputs()
    assert names, "training.csv has no forecast_inputs"
    nullable = nullable_inputs(load_schema("predict-request"))
    assert not nullable & set(names), f"forecast inputs that may be null: {sorted(nullable & set(names))}"


def test_exactly_the_14_named_inputs_are_nullable():
    nullable = nullable_inputs(load_schema("predict-request"))
    assert nullable == NULLABLE_INPUTS, (
        f"{len(nullable)} nullable inputs, expected {len(NULLABLE_INPUTS)}: "
        f"unexpected {sorted(nullable - NULLABLE_INPUTS)}, missing {sorted(NULLABLE_INPUTS - nullable)}")
    assert len(nullable) == 14


def test_bad_metric_objects_fail():
    for bad_metric in ({"value": 0.5, "reason": "insufficient data"}, {"value": None, "reason": None},
                       {"value": None, "reason": "because"}, {"value": 1.5, "reason": None},
                       {"value": 0.5}):
        bad = load_fixture("monitoring/2026-12-03.json")
        bad["windows"]["7d"]["f1"] = bad_metric
        assert errors("monitoring-output", bad), bad_metric


def test_bad_decisions_fail():
    good = load_fixture("monitoring/decisions/2026-12-03.json")
    for change in ({"retrain": False}, {"reasons": []}, {"forced": True}, {"cooldown_until": "tomorrow"},
                   {"thresholds_sha256": "abc"}, {"reasons": ["vibes"]}):
        assert errors("retrain-decision", {**good, **change}), change
    forced = {**good, "forced": True, "source": "manual", "reasons": ["manual"]}
    assert errors("retrain-decision", forced) == []
    assert errors("retrain-decision", {**forced, "reasons": ["fairness_gap"]})
    assert errors("retrain-decision", {**forced, "source": "live"})


BAD_TIMESTAMPS = ["2026-11-26 01:00:00", "2026-11-26T01:00:00", "2026-11-26T01:00:00+00:00", "2026-11-26T01:00Z"]


@pytest.mark.parametrize("bad_ts", BAD_TIMESTAMPS)
def test_a_bad_timestamp_fails_in_a_forecast_row_and_the_manifest(bad_ts):
    row = load_fixture(RECORDS)[0]
    assert errors("forecast-row", row) == []
    row["issued_at_utc"] = bad_ts
    assert errors("forecast-row", row), bad_ts
    manifest = load_fixture("manifests/latest.json")
    manifest["created_at_utc"] = bad_ts
    assert errors("data-manifest", manifest), bad_ts


@pytest.mark.parametrize("bad_id", ["ci-lightgbm", "3F9A1C07D2E84B56A0C1E7D94B2F6A85", "3f9a1c07", "", None])
def test_model_run_id_must_be_32_lowercase_hex(bad_id):
    response = load_fixture("predict/response.json")
    response["predictions"][0]["model_run_id"] = bad_id
    assert errors("predict-response", response), bad_id
    row = load_fixture(RECORDS)[0]
    row["model_run_id"] = bad_id
    assert errors("forecast-row", row), bad_id


def test_model_run_id_is_required_in_a_prediction():
    response = load_fixture("predict/response.json")
    del response["predictions"][0]["model_run_id"]
    assert errors("predict-response", response)


def test_bad_identifiers_fail():
    bad = load_fixture("predict/request.json")
    bad["data_version"] = "sha256:" + "A" * 64
    assert errors("predict-request", bad)
    bad = load_fixture("predict/response.json")
    bad["predictions"][0]["model_version"] = "surge-forecast@1"
    assert errors("predict-response", bad)
    bad = load_fixture("predict/request.json")
    bad["rows"][0]["forecast_date"] = "2026-02-30"
    assert errors("predict-request", bad)
    bad = load_fixture("manifests/latest.json")
    bad["vm_git_commit"] = "abc123"
    assert errors("data-manifest", bad)
    bad = load_fixture("manifests/latest.json")
    bad["files"]["labels"]["content_path"] = "dvc/files/md5/zz/short"
    assert errors("data-manifest", bad)
    bad = load_fixture("manifests/latest.json")
    entry = bad["files"]["features"]
    entry["content_path"] = entry["content_path"].removeprefix("gs://boston-resilience-twin-data/")  # not a full URI
    assert errors("data-manifest", bad)
    bad = load_fixture("manifests/latest.json")
    del bad["files"]["forecast_rows"]
    assert errors("data-manifest", bad)


# ---------------------------------------------------------------- catalog, units
def test_predict_request_inputs_equal_the_catalog():
    wanted = set(catalog_inputs())
    assert wanted
    schema = load_schema("predict-request")
    row_schema = schema["$defs"]["row"]
    keys = {"unit", "forecast_date", "as_of"}
    assert set(row_schema["properties"]) - keys == wanted
    assert set(row_schema["required"]) - keys == wanted
    for row in load_fixture("predict/request.json")["rows"]:
        assert set(row) - keys == wanted


def test_fixture_units_are_the_24_neighborhoods():
    units = sorted(poverty_units())
    assert len(units) == 24
    assert sorted(r["unit"] for r in load_fixture("predict/request.json")["rows"]) == units
    assert sorted(r["unit"] for r in load_fixture("predict/response.json")["predictions"]) == units
    assert [r["unit"] for r in load_fixture(RECORDS)] == units
    assert pq.read_table(FIXTURES / PARQUET).column("unit").to_pylist() == units


# ---------------------------------------------------------------- /predict
def test_predict_request_rules():
    request = load_fixture("predict/request.json")
    rows = request["rows"]
    assert request["data_version"] == data_version()
    assert len({r["unit"] for r in rows}) == len(rows), "units must be unique"
    assert len({r["forecast_date"] for r in rows}) == 1 and len({r["as_of"] for r in rows}) == 1
    for r in rows:
        assert date.fromisoformat(r["forecast_date"]) == date.fromisoformat(r["as_of"]) + timedelta(days=1)


def test_predict_response_matches_the_request_and_the_threshold():
    request, response = load_fixture("predict/request.json"), load_fixture("predict/response.json")
    assert [p["unit"] for p in response["predictions"]] == [r["unit"] for r in request["rows"]]
    for p, r in zip(response["predictions"], request["rows"], strict=True):
        assert p["date"] == r["forecast_date"]
        assert p["data_version"] == request["data_version"]
        # threshold.py: a surge is probability at or above the threshold (>=)
        assert p["prediction"] == int(p["probability"] >= p["threshold"])
    assert {p["threshold"] for p in response["predictions"]} == {THRESHOLD}
    assert {p["model_version"] for p in response["predictions"]} == {"1"}
    assert {p["model_run_id"] for p in response["predictions"]} == {RUN_ID}, "one model answers one request"


# ---------------------------------------------------------------- forecast file
def test_forecast_parquet_schema_and_metadata():
    parquet = pq.read_table(FIXTURES / PARQUET)
    assert parquet.schema.remove_metadata().equals(FORECAST_SCHEMA), parquet.schema
    assert parquet.schema.metadata[b"contract"] == b"forecast/v1"
    assert "contract" not in parquet.column_names
    assert parquet.num_rows == 24


def test_forecast_parquet_equals_the_records():
    parquet = pq.read_table(FIXTURES / PARQUET).to_pylist()
    as_json = [{
        **row,
        "forecast_date": row["forecast_date"].isoformat(),
        "as_of": row["as_of"].isoformat(),
        "issued_at_utc": row["issued_at_utc"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    } for row in parquet]
    assert as_json == load_fixture(RECORDS)


# ---------------------------------------------------------------- forecast rows file
def test_rows_file_columns_and_types_are_the_request_row_fields():
    table = rows_file()
    row_schema = load_schema("predict-request")["$defs"]["row"]
    assert table.column_names == [*row_schema["required"]], "the columns are exactly the request row fields"
    assert table.column_names[:3] == ["unit", "forecast_date", "as_of"]
    assert len(table.column_names) == 3 + 31
    for field in table.schema:
        if field.name == "unit":
            assert field.type == pa.string(), field
        elif field.name in ("forecast_date", "as_of"):
            assert field.type == pa.date32(), field
        elif field.name in ROWS_INT_COLUMNS:
            assert field.type == pa.int64(), field
        else:
            assert field.type == pa.float64(), field
    assert table.num_rows == 24
    units = table.column("unit").to_pylist()
    assert units == sorted(poverty_units()) == sorted(units), "one row per unit, sorted by unit"


def test_rows_file_nulls_are_only_where_the_request_allows_them():
    table = rows_file()
    nullable = nullable_inputs(load_schema("predict-request"))
    for name in table.column_names:
        if name not in nullable:
            assert table.column(name).null_count == 0, f"{name} has a null"


def test_rows_file_values_equal_the_request():
    rows = rows_file().to_pylist()
    as_json = [{**row, "forecast_date": row["forecast_date"].isoformat(), "as_of": row["as_of"].isoformat()}
               for row in rows]
    request = sorted(load_fixture("predict/request.json")["rows"], key=lambda r: r["unit"])
    assert as_json == request


def test_forecast_rules():
    records = load_fixture(RECORDS)
    units = [r["unit"] for r in records]
    assert units == sorted(units) and len(set(units)) == 24
    for key in ("forecast_date", "as_of", "threshold", "model_version", "model_run_id", "data_version",
                "issued_at_utc"):
        assert len({r[key] for r in records}) == 1, f"rows must share one {key}"
    for r in records:
        assert date.fromisoformat(r["forecast_date"]) == date.fromisoformat(r["as_of"]) + timedelta(days=1)
        assert r["prediction"] == int(r["probability"] >= r["threshold"])  # at or above, as threshold.py
        assert r["data_version"] == data_version()
        datetime.strptime(r["issued_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    assert any(r["probability"] == r["threshold"] and r["prediction"] == 1 for r in records), "no row on the threshold"
    assert any(r["prediction"] == 0 and r["probability"] > 0.09 for r in records), "no row just under it"


def test_forecast_agrees_with_the_predict_response():
    response = {p["unit"]: p for p in load_fixture("predict/response.json")["predictions"]}
    for r in load_fixture(RECORDS):
        p = response[r["unit"]]
        keys = ("probability", "prediction", "threshold", "model_version", "model_run_id", "data_version")
        assert {k: r[k] for k in keys} == {k: p[k] for k in keys}


# ---------------------------------------------------------------- manifest
@pytest.mark.parametrize("rel", ["manifests/2026-11-25.json", "manifests/latest.json"])
def test_manifest_is_in_canonical_form(rel):
    raw = (FIXTURES / rel).read_bytes()
    text = raw.decode("utf-8")
    no_floats(text)
    assert raw == canonical(json.loads(text))
    assert not raw.endswith(b"\n")
    assert "sha256:" not in text, "the manifest never contains its own hash"


def test_manifest_latest_is_the_dated_file_and_data_version_is_its_hash():
    dated = (FIXTURES / "manifests" / "2026-11-25.json").read_bytes()
    assert (FIXTURES / "manifests" / "latest.json").read_bytes() == dated
    computed = "sha256:" + hashlib.sha256(dated).hexdigest()
    assert (FIXTURES / "manifests" / "data_version.txt").read_text(encoding="utf-8") == computed
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", computed)


def test_manifest_content_paths_follow_the_dvc_remote():
    bucket, prefix = dvc_remote()
    manifest = load_fixture("manifests/latest.json")
    assert set(manifest["files"]) == {"labels", "features", "forecast_rows"}
    assert "forecast_rows" in load_schema("data-manifest")["properties"]["files"]["required"]
    for name, entry in manifest["files"].items():
        want = f"gs://{bucket}/{prefix}/files/md5/{entry['md5'][:2]}/{entry['md5'][2:]}"
        assert entry["content_path"] == want, name
    assert len({e["md5"] for e in manifest["files"].values()}) == 3, "three different files"
    assert manifest["as_of"] == "2026-11-25"
    assert date.fromisoformat(manifest["last_final_label_date"]) <= date.fromisoformat(manifest["as_of"]) - timedelta(days=2)


def test_every_fixture_carries_the_manifest_data_version():
    expected = data_version()
    seen = []
    for rel in ("predict/request.json", "forecasts/2026-11-26.brief.json", "monitoring/2026-11-27.json",
                "monitoring/2026-12-03.json"):
        assert load_fixture(rel)["data_version"] == expected, rel
        seen.append(rel)
    assert {p["data_version"] for p in load_fixture("predict/response.json")["predictions"]} == {expected}
    assert {r["data_version"] for r in load_fixture(RECORDS)} == {expected}
    assert set(pq.read_table(FIXTURES / PARQUET).column("data_version").to_pylist()) == {expected}
    assert len(seen) == 4


def test_model_version_is_the_same_everywhere():
    versions = {load_fixture(rel)["model_version"] for rel in (
        "forecasts/2026-11-26.brief.json", "monitoring/2026-11-27.json", "monitoring/2026-12-03.json")}
    versions |= {p["model_version"] for p in load_fixture("predict/response.json")["predictions"]}
    versions |= {r["model_version"] for r in load_fixture(RECORDS)}
    assert versions == {"1"}


# ---------------------------------------------------------------- metric objects, monitoring
@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json",
                                 "forecasts/2026-11-26.brief.json"])
def test_metric_objects_reason_is_null_iff_value_is_null(rel):
    found = metric_objects(load_fixture(rel))
    assert found
    for metric in found:
        assert (metric["reason"] is None) == (metric["value"] is not None), metric
        if metric["reason"] is not None:
            assert metric["reason"] in REASONS


def test_sparse_monitoring_fixture_is_insufficient_data():
    doc = load_fixture("monitoring/2026-11-27.json")
    for window in doc["windows"].values():
        assert window["positives"] < MIN_POSITIVES
        for key in ("f1", "precision", "recall"):
            assert window[key] == {"value": None, "reason": "insufficient data"}
    for group in doc["fairness"]["groups"].values():
        assert group["positives"] < MIN_POSITIVES
        assert group["recall"] == {"value": None, "reason": "insufficient data"}
    assert doc["fairness"]["gap"] == {"value": None, "reason": "insufficient data"}
    for group in doc["fairness"]["groups"].values():
        assert group["negatives"] == 0 and group["fp"] == 0
        assert group["fpr"] == {"value": None, "reason": "insufficient data"}
    assert doc["fairness"]["fpr_gap"] == {"value": None, "reason": "insufficient data"}


def test_rich_monitoring_fixture_has_numbers_and_some_drift():
    doc = load_fixture("monitoring/2026-12-03.json")
    for window in doc["windows"].values():
        assert window["positives"] >= MIN_POSITIVES
        for key in ("f1", "precision", "recall"):
            assert window[key]["value"] is not None
    assert doc["drift"]["drifted_features"] and doc["drift"]["share"]["value"] > 0
    assert doc["drift"]["prediction_drift"]["detected"] is True
    groups = doc["fairness"]["groups"]
    assert all(g["negatives"] >= MIN_POSITIVES and g["fpr"]["value"] is not None for g in groups.values())
    assert doc["fairness"]["fpr_gap"]["value"] >= 0.01, "a visible false-positive-rate gap"


def fpr_problems(doc: dict) -> list[str]:
    """The false-positive-rate rules of monitoring-output.md that a schema cannot say; empty when the file keeps them."""
    problems = []
    groups = doc["fairness"]["groups"]
    for name, group in groups.items():
        fp, negatives, fpr = group["fp"], group["negatives"], group["fpr"]
        if fp > negatives:
            problems.append(f"{name}: fp {fp} is more than negatives {negatives}")
        if negatives < MIN_POSITIVES:
            if fpr != {"value": None, "reason": "insufficient data"}:
                problems.append(f"{name}: fewer than 5 negatives needs a null fpr with insufficient data")
        elif fpr["value"] is None or not math.isclose(fpr["value"], fp / negatives, abs_tol=5e-5):
            problems.append(f"{name}: fpr is not fp / negatives")
        if fpr["reason"] == "no data":
            problems.append(f"{name}: fpr says no data")
    gap = doc["fairness"]["fpr_gap"]
    rates = [g["fpr"]["value"] for g in groups.values()]
    if None in rates:
        if gap != {"value": None, "reason": "insufficient data"}:
            problems.append("fpr_gap must be null with insufficient data when a group fpr is null")
    elif gap["value"] is None or not math.isclose(gap["value"], abs(groups["lower"]["fpr"]["value"]
                                                                   - groups["higher"]["fpr"]["value"]), abs_tol=1e-4):
        problems.append("fpr_gap is not |fpr(lower) - fpr(higher)|")
    if gap["reason"] == "no data":
        problems.append("fpr_gap says no data")
    if sum(g["fp"] for g in groups.values()) > doc["windows"]["30d"]["fp"]:
        problems.append("the groups' fp sum to more than windows.30d.fp")
    return problems


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_monitoring_fpr_rules(rel):
    doc = load_fixture(rel)
    assert fpr_problems(doc) == []
    assert set(doc["fairness"]["groups"]) == poverty_group_names()
    # the gap the recall rules check is still the recall gap
    assert set(doc["fairness"]) == {"groups", "gap", "fpr_gap"}


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_a_bad_fpr_fails(rel):
    def mutated(change):
        doc = load_fixture(rel)
        change(doc)
        return doc

    groups = lambda doc: doc["fairness"]["groups"]  # noqa: E731
    # the schema accepts each of these shapes; only the rules of the page catch them
    wrong_fpr = mutated(lambda d: groups(d)["higher"].update(fpr={"value": 0.5, "reason": None}))
    assert errors("monitoring-output", wrong_fpr) == [] and fpr_problems(wrong_fpr)
    fp_over = mutated(lambda d: groups(d)["lower"].update(fp=groups(d)["lower"]["negatives"] + 1))
    assert errors("monitoring-output", fp_over) == [] and fpr_problems(fp_over)
    few = mutated(lambda d: groups(d)["higher"].update(negatives=4, fp=1, fpr={"value": 0.25, "reason": None}))
    assert errors("monitoring-output", few) == [] and fpr_problems(few), "a non-null fpr with negatives < 5 passed"
    wrong_gap = mutated(lambda d: d["fairness"].update(fpr_gap={"value": 0.5, "reason": None}))
    assert errors("monitoring-output", wrong_gap) == [] and fpr_problems(wrong_gap)
    too_many_fp = mutated(lambda d: groups(d)["higher"].update(fp=d["windows"]["30d"]["fp"] + 1,
                                                               negatives=d["windows"]["30d"]["fp"] + 1))
    assert errors("monitoring-output", too_many_fp) == [] and fpr_problems(too_many_fp)
    no_data = mutated(lambda d: groups(d)["lower"].update(fpr={"value": None, "reason": "no data"}))
    assert errors("monitoring-output", no_data) == [] and fpr_problems(no_data)
    null_gap_with_rates = mutated(lambda d: d["fairness"].update(fpr_gap={"value": None, "reason": "undefined"}))
    assert fpr_problems(null_gap_with_rates), "a null gap with enough data, or the wrong reason, passed"
    for key in ("fp", "negatives", "fpr"):
        missing = mutated(lambda d: groups(d)["lower"].pop(key))
        assert errors("monitoring-output", missing), f"a group without {key} passed"
    assert errors("monitoring-output", mutated(lambda d: d["fairness"].pop("fpr_gap")))


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_monitoring_arithmetic_and_rules(rel):
    doc = load_fixture(rel)
    assert doc["date"] == Path(rel).stem
    for window in doc["windows"].values():
        tp, fp, fn = window["tp"], window["fp"], window["fn"]
        assert window["positives"] == tp + fn
        if window["positives"] < MIN_POSITIVES:  # the documented rule
            assert all(window[k]["reason"] == "insufficient data" for k in ("f1", "precision", "recall"))
        else:
            assert window["f1"]["value"] == pytest.approx(2 * tp / (2 * tp + fp + fn), abs=1e-4)
            assert window["precision"]["value"] == pytest.approx(tp / (tp + fp), abs=1e-4)
            assert window["recall"]["value"] == pytest.approx(tp / (tp + fn), abs=1e-4)
    assert doc["windows"]["30d"]["positives"] >= doc["windows"]["7d"]["positives"]
    groups = doc["fairness"]["groups"]
    assert set(groups) == poverty_group_names()
    assert sum(g["positives"] for g in groups.values()) <= doc["windows"]["30d"]["positives"]
    recalls = [g["recall"]["value"] for g in groups.values()]
    gap = doc["fairness"]["gap"]["value"]
    if None in recalls:
        assert gap is None
    else:
        assert gap == pytest.approx(abs(recalls[0] - recalls[1]), abs=5e-4)
    for group in groups.values():
        assert (group["recall"]["value"] is None) == (group["positives"] < MIN_POSITIVES)


def window_problems(doc: dict) -> list[str]:
    """The monitoring-output.md window rules a schema cannot say; empty when the file keeps them."""
    problems = []
    seven, thirty = doc["windows"]["7d"], doc["windows"]["30d"]
    if seven["end"] != thirty["end"]:
        problems.append("the two windows must share one end")
    end = date.fromisoformat(thirty["end"])
    if end > date.fromisoformat(doc["date"]) - timedelta(days=2):
        problems.append("end must be at most date - 2 days, so its labels are final")
    for name, days in (("7d", 7), ("30d", 30)):
        window = doc["windows"][name]
        if date.fromisoformat(window["end"]) - date.fromisoformat(window["start"]) != timedelta(days=days - 1):
            problems.append(f"{name} must hold {days} forecast dates, start = end - {days - 1} days")
    return problems


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_monitoring_windows_follow_the_rules(rel):
    doc = load_fixture(rel)
    assert window_problems(doc) == []
    assert doc["windows"]["7d"]["end"] == (date.fromisoformat(doc["date"]) - timedelta(days=2)).isoformat()


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_a_bad_monitoring_window_fails(rel):
    seven_days_of_eight = load_fixture(rel)
    start = date.fromisoformat(seven_days_of_eight["windows"]["7d"]["start"])
    seven_days_of_eight["windows"]["7d"]["start"] = (start - timedelta(days=1)).isoformat()
    assert errors("monitoring-output", seven_days_of_eight) == [], "the schema cannot see the length"
    assert window_problems(seven_days_of_eight), "a 7d window of 8 days passed"

    thirty_of_31 = load_fixture(rel)
    start = date.fromisoformat(thirty_of_31["windows"]["30d"]["start"])
    thirty_of_31["windows"]["30d"]["start"] = (start - timedelta(days=1)).isoformat()
    assert window_problems(thirty_of_31)

    apart = load_fixture(rel)
    apart["windows"]["30d"]["end"] = (date.fromisoformat(apart["windows"]["30d"]["end"]) - timedelta(days=1)).isoformat()
    assert window_problems(apart), "windows with different ends passed"

    too_new = load_fixture(rel)
    day_before = (date.fromisoformat(too_new["date"]) - timedelta(days=1)).isoformat()
    for window in too_new["windows"].values():
        window["end"] = day_before
    assert window_problems(too_new), "an end whose labels are not final passed"

    for key in ("start", "end"):
        missing = load_fixture(rel)
        del missing["windows"]["7d"][key]
        assert errors("monitoring-output", missing), f"a window without {key} passed"


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_no_performance_or_fairness_metric_says_no_data(rel):
    doc = load_fixture(rel)
    for part in ("windows", "fairness"):
        for metric in metric_objects(doc[part]):
            assert metric["reason"] != "no data", (rel, part, metric)
    assert doc["drift"]["share"]["reason"] in (None, "no data")


@pytest.mark.parametrize("rel", ["monitoring/2026-11-27.json", "monitoring/2026-12-03.json"])
def test_drifted_features_are_catalog_inputs(rel):
    drift = load_fixture(rel)["drift"]
    inputs = catalog_inputs()
    assert set(drift["drifted_features"]) <= set(inputs)
    assert drift["drifted_features"] == sorted(drift["drifted_features"], key=inputs.index), "catalog order"
    if drift["share"]["value"] is not None:
        assert drift["share"]["value"] == pytest.approx(len(drift["drifted_features"]) / len(inputs), abs=1e-4)


# ---------------------------------------------------------------- decisions
def test_decision_cross_field_rules():
    for rel in ("monitoring/decisions/2026-11-27.json", "monitoring/decisions/2026-12-03.json",
                "monitoring/decisions/latest.json"):
        doc = load_fixture(rel)
        assert doc["retrain"] == bool(doc["reasons"]), rel
        if doc["forced"]:
            assert doc["source"] == "manual" and "manual" in doc["reasons"], rel
    assert load_fixture("monitoring/decisions/2026-11-27.json")["retrain"] is False
    assert load_fixture("monitoring/decisions/2026-12-03.json")["retrain"] is True


def test_the_decisions_folder_holds_only_live_or_manual_decisions():
    folder = FIXTURES / "monitoring" / "decisions"
    files = sorted(folder.glob("*.json"))
    assert files
    for path in files:
        assert json.loads(path.read_text(encoding="utf-8"))["source"] in ("live", "manual"), path.name
    replay = load_fixture("monitoring/decisions/2026-11-27.json")
    replay["source"] = "replay"
    assert errors("retrain-decision", replay) == [], "a replay decision is valid, only not written to this folder"


def test_decisions_dates_and_latest():
    folder = FIXTURES / "monitoring" / "decisions"
    dated = sorted(p for p in folder.glob("*.json") if p.name != "latest.json")
    assert len(dated) == 2
    for path in dated:
        assert json.loads(path.read_text(encoding="utf-8"))["date"] == path.stem
    assert (folder / "latest.json").read_bytes() == dated[-1].read_bytes()
    first, last = (json.loads(p.read_text(encoding="utf-8")) for p in dated)
    assert first["thresholds_sha256"] == last["thresholds_sha256"]


# ---------------------------------------------------------------- brief
def _number_token(display: str) -> re.Pattern:
    return re.compile(r"(?<![\w.])" + re.escape(display) + r"(?!\w|\.\d)")


def _decimals(display: str) -> int:
    return len(display.split(".")[1]) if "." in display else 0


def _fixture_of(uri: str) -> str:
    """The fixture path of a gs:// source: the bucket prefix stripped, as the folder mirrors the bucket."""
    bucket, _ = dvc_remote()
    assert uri.startswith(f"gs://{bucket}/"), uri
    return uri.removeprefix(f"gs://{bucket}/")


def _forecast_rows_path() -> str:
    return load_fixture("manifests/latest.json")["files"]["forecast_rows"]["content_path"]


def _source_value(claim: dict):
    """The value a claim names in the file it cites: the forecast fixture, a monitoring fixture, or the row of the
    forecast rows fixture (the manifest's forecast_rows) by unit and forecast_date."""
    src = claim["source"]
    rel = _fixture_of(src["file"])
    if rel == PARQUET:
        row = next(r for r in pq.read_table(FIXTURES / PARQUET).to_pylist()
                   if r["unit"] == src["row"]["unit"] and r["forecast_date"].isoformat() == src["row"]["date"])
        return row[src["field"]]
    if rel.startswith("monitoring/"):
        return get_path(load_fixture(rel), src["field"])
    assert src["file"] == _forecast_rows_path(), f"a source that is not an allowed file: {src['file']}"
    row = next(r for r in rows_file().to_pylist()
               if r["unit"] == src["row"]["unit"] and r["forecast_date"].isoformat() == src["row"]["date"])
    return row[src["field"]]


def test_brief_critic_rule_on_the_fixture():
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    ids = [c["id"] for c in brief["claims"]]
    assert len(ids) == len(set(ids)) and brief["claims"]
    for claim in brief["claims"]:
        assert claim["display"] == f"{claim['value']:.{_decimals(claim['display'])}f}", claim
        assert _number_token(claim["display"]).search(brief["text"]), claim
    assert brief["forecast_date"] == "2026-11-26"


def test_brief_claims_match_their_sources():
    """Every claim is checked: a claim whose source is not one of the three allowed files fails, and so does one
    left unchecked."""
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    rows_units = set(rows_file().column("unit").to_pylist())
    inputs = set(catalog_inputs())
    forecast_date = brief["forecast_date"]
    unchecked = {c["id"] for c in brief["claims"]}
    for claim in brief["claims"]:
        src = claim["source"]
        expected = _source_value(claim)  # fails for a file that is not allowed
        assert claim["value"] == expected, claim
        if src["file"] == _forecast_rows_path():
            assert src["field"] in inputs, claim
            assert src["row"]["date"] == forecast_date, claim
            assert src["row"]["unit"] in rows_units, claim
        elif _fixture_of(src["file"]) == PARQUET:
            assert src["row"]["date"] == forecast_date, claim
            assert src["field"] in FORECAST_SCHEMA.names, claim
        else:
            assert src["row"] is None, claim
        unchecked.discard(claim["id"])
    assert not unchecked, f"claims left unchecked: {sorted(unchecked)}"


def test_brief_association_claims_cite_the_forecast_rows_file():
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    rows_uri = _forecast_rows_path()
    request = {r["unit"]: {**r, "forecast_date": r["forecast_date"].isoformat()} for r in rows_file().to_pylist()}
    on_rows = [c for c in brief["claims"] if c["source"]["file"] == rows_uri]
    assert on_rows
    cited = {(c["source"]["row"]["unit"], c["source"]["field"], c["value"]) for c in on_rows}
    for flag in brief["flagged"]:
        assert flag["associations"], flag["unit"]
        for a in flag["associations"]:
            assert (flag["unit"], a["feature"], a["value"]) in cited, f"no claim for {flag['unit']} {a['feature']}"
            assert request[flag["unit"]][a["feature"]] == a["value"], (flag["unit"], a["feature"])
    for c in on_rows:
        assert request[c["source"]["row"]["unit"]][c["source"]["field"]] == c["value"], c
        assert c["source"]["row"]["date"] == brief["forecast_date"] == request[c["source"]["row"]["unit"]]["forecast_date"]


def test_brief_sources_outside_the_three_allowed_files_fail():
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    bucket = "gs://" + dvc_remote()[0] + "/"
    assert errors("agent-brief", _with_source_file(brief, bucket + "forecasts/2026-11-26.parquet")) == []
    for bad in ("forecasts/2026-11-26.parquet", "Data-Pipeline/data/processed/features.parquet",
                "dvc/files/md5/41/b7640ca7f5feb4b27f3c38437a6546", bucket + "forecasts/2026-11-26.records.json",
                bucket + "manifests/latest.json"):
        assert errors("agent-brief", _with_source_file(brief, bad)), bad  # not a full URI of an allowed kind
    wrong_file = _with_source_file(brief, bucket + "dvc/files/md5/41/b7640ca7f5feb4b27f3c38437a6546")
    with pytest.raises(AssertionError, match="not an allowed file"):
        _source_value(wrong_file["claims"][0])  # a valid-looking DVC path, but not the manifest's forecast_rows


def _with_source_file(brief: dict, file: str) -> dict:
    bad = copy.deepcopy(brief)
    bad["claims"][0]["source"]["file"] = file
    return bad


def test_brief_a_wrong_input_claim_is_caught():
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    claim = next(c for c in brief["claims"] if c["source"]["file"] == _forecast_rows_path())
    wrong = copy.deepcopy(claim)
    wrong["value"] += 0.5
    assert wrong["value"] != _source_value(wrong)
    wrong = copy.deepcopy(claim)
    wrong["source"]["row"]["unit"] = "Allston"
    assert claim["value"] != _source_value(wrong), "the row, not only the field, picks the value"


def test_brief_flagged_matches_the_forecast_and_health_is_unmeasured():
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    flagged = [r for r in load_fixture(RECORDS) if r["prediction"] == 1]
    flagged.sort(key=lambda r: (-r["probability"], r["unit"]))
    assert [(f["unit"], f["probability"]) for f in brief["flagged"]] == [(r["unit"], r["probability"]) for r in flagged]
    assert brief["data_version"] == data_version() and brief["model_version"] == "1"
    claimed = {(c["source"]["field"], c["value"]) for c in brief["claims"]}
    for f in brief["flagged"]:
        for a in f["associations"]:
            assert (a["feature"], a["value"]) in claimed, a
    assert all(v["reason"] == "no data" for v in brief["model_health"].values())


def test_brief_critic_catches_a_wrong_claim():
    brief = load_fixture("forecasts/2026-11-26.brief.json")
    wrong = copy.deepcopy(brief["claims"][1])
    wrong["value"] += 0.01
    assert wrong["value"] != _source_value(wrong)
    wrong = copy.deepcopy(brief["claims"][1])
    wrong["display"] = "0.99"
    assert wrong["display"] != f"{wrong['value']:.2f}"
    assert not _number_token("0.99").search(brief["text"])
    assert not _number_token("0.1").search("probability 0.14"), "a prefix of a longer number is not a match"


# ---------------------------------------------------------------- metric names
def test_metric_names_descriptors():
    doc = load_fixture("metrics/metric-names.json")
    names = [m["name"] for m in doc["metrics"]]
    assert names == ["f1", "precision", "recall", "positives", "fairness_gap", "group_fpr", "fpr_gap", "drift_share",
                     "retrain_decision", "forecast_freshness_hours"]
    by_name = {m["name"]: m for m in doc["metrics"]}
    assert by_name["fpr_gap"]["labels"] == [] and by_name["fairness_gap"]["labels"] == []
    for name in ("group_fpr", "fpr_gap"):
        assert (by_name[name]["metric_kind"], by_name[name]["value_type"], by_name[name]["unit"]) == (
            "GAUGE", "DOUBLE", "1")
    for name in ("f1", "precision", "recall", "positives"):
        assert by_name[name]["labels"] == [{"key": "window", "allowed_values": ["7d", "30d"]}]
    assert by_name["retrain_decision"]["labels"] == [{"key": "source", "allowed_values": ["live", "replay", "manual"]}]
    # the group label's values are the group keys of poverty_groups.csv, read now
    assert len(by_name["group_fpr"]["labels"]) == 1 and by_name["group_fpr"]["labels"][0]["key"] == "group"
    assert set(by_name["group_fpr"]["labels"][0]["allowed_values"]) == poverty_group_names()
    assert set(load_fixture("monitoring/2026-12-03.json")["fairness"]["groups"]) == poverty_group_names()
    page = (CONTRACTS / "metric-names.md").read_text(encoding="utf-8")
    assert f"one of the {NUMBER_WORDS[len(names)]} below" in page, "the page's count of metrics"
    for name in names:
        assert f"| `{name}` |" in page, name
    assert {m["producer"] for m in doc["metrics"]} == {"#169"}, "every metric, freshness included, is #169's"
    # the windows and sources the labels allow are the ones the other contracts use
    assert set(load_fixture("monitoring/2026-12-03.json")["windows"]) == {"7d", "30d"}
    schema = load_schema("retrain-decision")
    assert set(schema["properties"]["source"]["enum"]) == {"live", "replay", "manual"}


# ---------------------------------------------------------------- the fixture tool
def _run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BUILD_FIXTURES), *args], capture_output=True, text=True, check=False,
                          timeout=120)


def test_build_fixtures_check_passes_on_the_committed_fixtures():
    result = _run_tool("--check")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("rel,change", [
    ("predict/request.json", lambda d: d.update(data_version="sha256:" + "1" * 64)),  # a stale data_version
    ("forecasts/2026-11-26.records.json", lambda d: d[0].update(probability=0.0123)),  # parquet is now stale
    ("monitoring/2026-12-03.json", lambda d: d.update(data_version="sha256:" + "0" * 64)),  # a stale data_version
])
def test_build_fixtures_check_fails_on_a_changed_fixture_and_writes_nothing(tmp_path, rel, change):
    """Mutate a copy of the fixtures, never the real ones."""
    copy_of = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copy_of)
    path = copy_of / rel
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    before = {p.relative_to(copy_of): p.read_bytes() for p in copy_of.rglob("*") if p.is_file()}
    result = _run_tool("--check", "--fixtures", str(copy_of))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "would change" in result.stdout
    assert {p.relative_to(copy_of): p.read_bytes() for p in copy_of.rglob("*") if p.is_file()} == before


def test_build_fixtures_check_names_a_stale_rows_file_and_writes_it_back(tmp_path):
    copy_of = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copy_of)
    path = copy_of / "predict" / "request.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["rows"][0]["wx_precip_mm"] += 1
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result = _run_tool("--check", "--fixtures", str(copy_of))
    assert result.returncode == 1 and ROWS_PARQUET in result.stdout, result.stdout
    (copy_of / ROWS_PARQUET).unlink()
    assert _run_tool("--fixtures", str(copy_of)).returncode == 0, "the tool writes a missing rows file"
    assert _run_tool("--check", "--fixtures", str(copy_of)).returncode == 0


def test_build_fixtures_check_fails_on_a_non_canonical_file(tmp_path):
    copy_of = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copy_of)
    path = copy_of / "predict" / "response.json"
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")  # one line, no newline
    result = _run_tool("--check", "--fixtures", str(copy_of))
    assert result.returncode == 1 and "predict/response.json" in result.stdout, result.stdout
    manifest = copy_of / "manifests" / "2026-11-25.json"
    manifest.write_text(json.dumps(json.loads(manifest.read_text(encoding="utf-8")), indent=2), encoding="utf-8")
    result = _run_tool("--check", "--fixtures", str(copy_of))
    assert "manifests/2026-11-25.json" in result.stdout, "a pretty-printed manifest is not canonical"


def test_build_fixtures_check_names_a_changed_manifest_and_what_follows_from_it(tmp_path):
    copy_of = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copy_of)
    manifest = copy_of / "manifests" / "2026-11-25.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["files"]["labels"]["size_bytes"] += 1
    manifest.write_bytes(canonical(doc))
    result = _run_tool("--check", "--fixtures", str(copy_of))
    assert result.returncode == 1
    for name in ("manifests/latest.json", "manifests/data_version.txt", "predict/request.json",
                 "forecasts/2026-11-26.parquet", "forecasts/2026-11-26.brief.json"):
        assert name in result.stdout, name
    # writing it brings every file back in line, and a second check then passes
    assert _run_tool("--fixtures", str(copy_of)).returncode == 0
    assert _run_tool("--check", "--fixtures", str(copy_of)).returncode == 0
    new = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert (copy_of / "manifests" / "data_version.txt").read_text(encoding="utf-8") == new
    assert json.loads((copy_of / "predict" / "request.json").read_text(encoding="utf-8"))["data_version"] == new


# ---------------------------------------------------------------- the pages
@pytest.mark.parametrize("page", sorted(PAGES))
def test_page_has_every_section_and_links_its_schema_and_fixtures(page):
    text = (CONTRACTS / page).read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, flags=re.M)
    assert headings == SECTIONS, headings
    schemas, fixtures = PAGES[page]
    for name in schemas:
        assert f"schemas/{name}.schema.json" in text, f"{page} does not link {name}"
        assert (SCHEMAS / f"{name}.schema.json").is_file()
    for rel in fixtures:
        assert f"fixtures/{rel}" in text, f"{page} does not link fixtures/{rel}"
        assert (FIXTURES / rel).is_file()


def test_readme_lists_all_seven_contracts():
    text = (CONTRACTS / "README.md").read_text(encoding="utf-8")
    for page in PAGES:
        assert f"]({page})" in text, f"README.md does not link {page}"
    for needle in ("Producer", "Consumers", "#160", "#161", "#162", "#163", "#166", "#167", "#169", "#170", "#171",
                   "#172", "#173", "#174", "synthetic"):
        assert needle in text, needle


def test_every_fixture_file_is_covered_by_a_test():
    on_disk = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file()}
    covered = set(JSON_FIXTURES) | {RECORDS, PARQUET, ROWS_PARQUET, "manifests/data_version.txt"}
    assert on_disk == covered
