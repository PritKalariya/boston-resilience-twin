"""Score tomorrow's forecast rows on the model service and write the forecast file (#162).

The contracts are docs/contracts/predict.md and docs/contracts/forecast-file.md (#159).

1. The rows are Data-Pipeline/data/forecast/<as_of>/features.parquet, which forecast_rows (#160)
   writes and the manifest names as files.forecast_rows. They are sent as they are, so the manifest's
   md5 names exactly what was scored, and the file must still have that md5 when it is read here.
   They must be all 24 units, once each, for one as_of and forecast_date = as_of + 1.
2. Scoring stops, naming every such column, when a model input is null in more than half of the
   rows: the failure alert then says which inputs are missing (#162).
3. The request carries the data_version of manifests/<as_of>.json as it is in the bucket (the
   manifest task runs first), and is checked against docs/contracts/schemas/predict-request.schema.json
   before it is sent, so a null the contract does not allow is caught here, with its column, instead
   of coming back as the service's 422.
4. POST <predict_url>/predict, with an ID token for the service URL from the VM's service account
   (the metadata server). A 5xx, a 429, a timeout or a lost connection is retried after each wait in
   evening_run.csv (a cold start or a model still loading is a 503); a 401 or 403 means airflow-vm-sa
   lacks roles/run.invoker on the service; any other 4xx fails at once with the service's detail,
   since sending the same body again cannot fix it.
5. The response is checked against its schema and the contract's rules: one prediction per row in
   the request's order, the same unit and date, the data_version echoed, one model, and
   prediction = 1 exactly when probability >= threshold.
6. The forecast file: 24 rows with the contract's Arrow schema and contract=forecast/v1 key-value
   metadata, written with pyarrow (a pandas round trip can drop the metadata), with issued_at_utc in
   whole seconds. It is kept as forecast.parquet beside the rows and written to
   forecasts/<forecast_date>.parquet in the bucket, where a rerun replaces it (last write wins).

check_forecast() is the 20:00 check: tomorrow's forecast file must be in the bucket.

pyarrow, jsonschema, requests and google-auth are imported inside the functions that use them, so
the DAG can import this module at parse time.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import evening_run
from data_bucket import DataBucket
from live_manifest import data_version, file_md5, forecast_dir, manifest_path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "docs" / "contracts" / "schemas"
CONTRACT = "predict/v1"
FORECAST_CONTRACT = "forecast/v1"
KEYS = ("unit", "forecast_date", "as_of")
UNITS = 24
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PARQUET = "application/vnd.apache.parquet"
SETTINGS_FILE = "Data-Pipeline/config/evening_run.csv"

log = logging.getLogger(__name__)


class ScoringError(RuntimeError):
    """The forecast cannot be made as the contracts require; nothing was written to forecasts/."""


class MissingInputs(ScoringError):
    """More than half of tomorrow's rows have no value for some model input."""


class ForecastMissing(RuntimeError):
    """Tomorrow's forecast file is not in the bucket."""


def forecast_path(forecast_date: date) -> str:
    return f"forecasts/{forecast_date.isoformat()}.parquet"


def json_value(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def read_rows(path: Path) -> list[dict]:
    """The forecast rows as the request's JSON rows: dates as ISO strings, a NaN as null."""
    import pyarrow.parquet as pq

    if not path.is_file():
        raise ScoringError(f"no forecast rows at {path}; forecast_rows (#160) writes them on an evening run")
    return [{name: json_value(value) for name, value in row.items()} for row in pq.read_table(path).to_pylist()]


def check_rows(rows: list[dict], as_of: date) -> date:
    """All 24 units once, one as_of and one forecast_date = as_of + 1. Returns the forecast date."""
    if len(rows) != UNITS:
        raise ScoringError(f"{len(rows)} forecast rows, not {UNITS}: the forecast file needs every neighborhood")
    units = [row.get("unit") for row in rows]
    repeated = sorted({unit for unit in units if units.count(unit) > 1})
    if repeated:
        raise ScoringError(f"units repeated in the forecast rows: {repeated}")
    forecast_date = as_of + timedelta(days=1)
    as_ofs = {row.get("as_of") for row in rows}
    dates = {row.get("forecast_date") for row in rows}
    if as_ofs != {as_of.isoformat()} or dates != {forecast_date.isoformat()}:
        raise ScoringError(f"the forecast rows are for as_of {sorted(map(str, as_ofs))} and forecast_date "
                           f"{sorted(map(str, dates))}, not {as_of} and {forecast_date}")
    return forecast_date


def mostly_missing(rows: list[dict]) -> list[str]:
    """Each model input null in more than half of the rows, with its count."""
    inputs = [name for name in rows[0] if name not in KEYS]
    counts = {name: sum(row.get(name) is None for row in rows) for name in inputs}
    return [f"{name} ({count} of {len(rows)})" for name, count in counts.items() if count * 2 > len(rows)]


def stop_if_mostly_missing(rows: list[dict]) -> None:
    found = mostly_missing(rows)
    if found:
        raise MissingInputs("stopped before scoring: more than half of tomorrow's rows have no value for "
                            + ", ".join(found))


def request_body(rows: list[dict], version: str) -> dict:
    return {"contract": CONTRACT, "data_version": version, "rows": rows}


def validate(document: dict, schema_name: str, what: str) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
                    key=lambda e: [str(part) for part in e.absolute_path])
    if errors:
        shown = [f"{'/'.join(str(part) for part in e.absolute_path) or 'the body'}: {e.message}" for e in errors[:5]]
        more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
        raise ScoringError(f"the {what} breaks its contract ({schema_name}): " + "; ".join(shown) + more)


def id_token(audience: str) -> str:
    """An ID token for the service from the VM's service account (Cloud Run checks the audience)."""
    import google.auth.exceptions
    import google.auth.transport.requests
    import google.oauth2.id_token

    try:
        return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
    except google.auth.exceptions.GoogleAuthError as e:
        raise ScoringError(f"could not get an ID token for {audience}: {e}") from None


def post_predict(url: str, body: dict, config: evening_run.Settings, *, token=None, session=None,
                 sleep=time.sleep) -> dict:
    """POST the body to <url>/predict, retrying what a retry can fix. Returns the 200 response's JSON."""
    import requests

    session = session or requests.Session()
    token = token or id_token
    endpoint = f"{url}/predict"
    tries = len(config.retry_waits) + 1
    last = ""
    for attempt in range(tries):
        if attempt:
            sleep(config.retry_waits[attempt - 1])
        try:
            response = session.post(endpoint, json=body, headers={"Authorization": f"Bearer {token(url)}"},
                                    timeout=config.timeout_seconds)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = f"{type(e).__name__}: {e}"
            log.warning("/predict try %d of %d: %s", attempt + 1, tries, last)
            continue
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                raise ScoringError(f"{endpoint} answered 200 with a body that is not JSON") from None
        detail = response.text[:500]
        if response.status_code in RETRY_STATUSES:
            last = f"HTTP {response.status_code}: {detail}"
            log.warning("/predict try %d of %d: %s", attempt + 1, tries, last)
            continue
        if response.status_code in (401, 403):
            raise ScoringError(f"{endpoint} refused the VM's identity (HTTP {response.status_code}); airflow-vm-sa "
                               f"needs roles/run.invoker on the service (#161): {detail}")
        raise ScoringError(f"{endpoint} rejected the request (HTTP {response.status_code}), so it is not "
                           f"retried: {detail}")
    raise ScoringError(f"no answer from {endpoint} after {tries} tries; the last: {last}")


def check_response(body: dict, response: dict) -> None:
    validate(response, "predict-response.schema.json", "response")
    rows, predictions = body["rows"], response["predictions"]
    if len(predictions) != len(rows):
        raise ScoringError(f"{len(predictions)} predictions for {len(rows)} rows")
    for i, (row, prediction) in enumerate(zip(rows, predictions)):
        if (prediction["unit"], prediction["date"]) != (row["unit"], row["forecast_date"]):
            raise ScoringError(f"prediction {i} is for {prediction['unit']} on {prediction['date']}, not "
                               f"{row['unit']} on {row['forecast_date']}")
        if prediction["data_version"] != body["data_version"]:
            raise ScoringError(f"prediction {i} carries data_version {prediction['data_version']}, not the "
                               f"request's {body['data_version']}")
        if prediction["prediction"] != int(prediction["probability"] >= prediction["threshold"]):
            raise ScoringError(f"prediction {i} ({prediction['unit']}) is {prediction['prediction']} at probability "
                               f"{prediction['probability']} and threshold {prediction['threshold']}")
    models = {(p["model_version"], p["model_run_id"], p["threshold"]) for p in predictions}
    if len(models) != 1:
        raise ScoringError(f"more than one model answered: {sorted(models)}")


def forecast_schema():
    import pyarrow as pa

    return pa.schema([
        ("unit", pa.string()), ("forecast_date", pa.date32()), ("as_of", pa.date32()), ("probability", pa.float64()),
        ("prediction", pa.int8()), ("threshold", pa.float64()), ("model_version", pa.string()),
        ("model_run_id", pa.string()), ("data_version", pa.string()), ("issued_at_utc", pa.timestamp("us", tz="UTC"))])


def forecast_table(body: dict, response: dict, issued_at: datetime):
    """The forecast file's table: one row per unit, sorted by unit, with the contract's metadata."""
    import pyarrow as pa

    issued = issued_at.astimezone(timezone.utc).replace(microsecond=0)
    pairs = sorted(zip(body["rows"], response["predictions"]), key=lambda pair: pair[0]["unit"])
    columns = {
        "unit": [row["unit"] for row, _ in pairs],
        "forecast_date": [date.fromisoformat(row["forecast_date"]) for row, _ in pairs],
        "as_of": [date.fromisoformat(row["as_of"]) for row, _ in pairs],
        "probability": [float(p["probability"]) for _, p in pairs],
        "prediction": [p["prediction"] for _, p in pairs],
        "threshold": [float(p["threshold"]) for _, p in pairs],
        "model_version": [p["model_version"] for _, p in pairs],
        "model_run_id": [p["model_run_id"] for _, p in pairs],
        "data_version": [p["data_version"] for _, p in pairs],
        "issued_at_utc": [issued] * len(pairs),
    }
    return pa.table(columns, schema=forecast_schema()).replace_schema_metadata({"contract": FORECAST_CONTRACT})


def score(as_of: date, repo: Path = REPO_ROOT, bucket: DataBucket | None = None,
          config: evening_run.Settings | None = None, post=None, now: datetime | None = None) -> dict:
    """Make and write the forecast for as_of + 1. Returns what the Slack summary needs."""
    import pyarrow.parquet as pq

    config = config or evening_run.settings()
    if not config.predict_url:
        raise ScoringError(f"predict_url is blank in {SETTINGS_FILE}; set it to the brt-model service URL (#161)")
    rows_path = forecast_dir(as_of, repo) / "features.parquet"
    rows = read_rows(rows_path)
    forecast_date = check_rows(rows, as_of)
    stop_if_mostly_missing(rows)

    bucket = bucket or DataBucket.from_repo(repo)
    manifest = bucket.read(manifest_path(as_of))
    if manifest is None:
        raise ScoringError(f"{bucket.uri(manifest_path(as_of))} does not exist; the manifest task runs before scoring")
    named = json.loads(manifest)["files"]["forecast_rows"]["md5"]
    if file_md5(rows_path) != named:
        raise ScoringError(f"{rows_path} changed since the manifest named it (md5 {named}); rerun the whole evening")
    body = request_body(rows, data_version(manifest))
    validate(body, "predict-request.schema.json", "request")

    response = (post or post_predict)(config.predict_url, body, config)
    check_response(body, response)
    table = forecast_table(body, response, now or datetime.now(timezone.utc))
    local = forecast_dir(as_of, repo) / "forecast.parquet"
    pq.write_table(table, local)
    bucket.write(forecast_path(forecast_date), local.read_bytes(), PARQUET)

    predictions = response["predictions"]
    flagged = [p["unit"] for p in sorted(predictions, key=lambda p: (-p["probability"], p["unit"])) if p["prediction"]]
    log.info("wrote %s: %d of %d neighborhoods flagged, model version %s, %s", bucket.uri(forecast_path(forecast_date)),
             len(flagged), len(predictions), predictions[0]["model_version"], body["data_version"])
    return {
        "forecast_date": forecast_date.isoformat(),
        "flagged": flagged,
        "units": len(predictions),
        "model_version": predictions[0]["model_version"],
        "model_run_id": predictions[0]["model_run_id"],
        "data_version": body["data_version"],
        "forecast_uri": bucket.uri(forecast_path(forecast_date)),
    }


def check_forecast(forecast_date: date, bucket: DataBucket | None = None, repo: Path = REPO_ROOT) -> str:
    """The 20:00 check: the forecast file for forecast_date must be in the bucket. Returns its URI."""
    bucket = bucket or DataBucket.from_repo(repo)
    path = forecast_path(forecast_date)
    if not bucket.exists(path):
        raise ForecastMissing(f"{bucket.uri(path)} is not in the bucket at the 20:00 check: no forecast for {forecast_date}")
    return bucket.uri(path)
