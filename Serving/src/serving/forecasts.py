"""Read one day's forecast file (docs/contracts/forecast-file.md) and give its rows as JSON.

FORECASTS_ROOT is a local folder or gs://<bucket>/<prefix>. A bucket is read through the Cloud Storage
JSON API (alt=media) with google-auth's AuthorizedSession, as Model-Development/scripts/vertex_client.py
reads Google's APIs, so there is no storage client library. Readers check the file's key-value metadata
before its columns.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pyarrow.parquet as pq
import requests
from google.auth.exceptions import GoogleAuthError

CONTRACT_KEY = b"contract"
CONTRACT_VALUE = b"forecast/v1"
SCOPES = ("https://www.googleapis.com/auth/devstorage.read_only",)
TIMEOUT_SECONDS = 30
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ForecastNotFound(Exception):
    """No file for the date."""


class ForecastUnreadable(Exception):
    """A file or object that cannot be served; the message is the reason."""


class ForecastStorageError(Exception):
    """The store refused or failed; the message names the call and its status."""


def default_session():
    """An authorized requests session from Application Default Credentials."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=list(SCOPES))
    return AuthorizedSession(credentials)


def _split_gs(root: str) -> tuple[str, str]:
    bucket, _, prefix = root.removeprefix("gs://").partition("/")
    if not bucket:
        raise ForecastStorageError(f"FORECASTS_ROOT {root!r} names no bucket")
    return bucket, prefix.strip("/")


def fetch(root: str, date: str, session=None) -> bytes:
    """The bytes of <root>/<date>.parquet. date has already been checked as YYYY-MM-DD."""
    if root.startswith("gs://"):
        bucket, prefix = _split_gs(root)
        name = f"{prefix}/{date}.parquet" if prefix else f"{date}.parquet"
        url = f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o/{quote(name, safe='')}?alt=media"
        try:
            response = session.get(url, timeout=TIMEOUT_SECONDS)
        except (requests.RequestException, GoogleAuthError) as exc:  # the store cannot be reached or refuses a token
            raise ForecastStorageError(f"cannot read gs://{bucket}/{name}: {type(exc).__name__}: {exc}") from None
        if response.status_code == 404:
            raise ForecastNotFound(f"no forecast for {date}")
        if response.status_code != 200:
            raise ForecastStorageError(f"reading gs://{bucket}/{name} failed with status {response.status_code}")
        return response.content
    path = Path(root) / f"{date}.parquet"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise ForecastNotFound(f"no forecast for {date}") from None
    except OSError as exc:
        raise ForecastStorageError(f"cannot read {path}: {exc}") from None


def records(data: bytes) -> list[dict]:
    """The rows of a forecast file as the JSON of records.json: dates as YYYY-MM-DD, issued_at_utc as
    YYYY-MM-DDTHH:MM:SSZ. A file without contract=forecast/v1 metadata is refused."""
    try:
        table = pq.read_table(io.BytesIO(data))
    except Exception as exc:
        raise ForecastUnreadable(f"the forecast file is not readable parquet: {type(exc).__name__}") from None
    metadata = table.schema.metadata or {}
    if metadata.get(CONTRACT_KEY) != CONTRACT_VALUE:
        raise ForecastUnreadable("the forecast file has no contract=forecast/v1 key-value metadata")
    rows = table.to_pylist()
    for row in rows:
        for key, value in row.items():
            if isinstance(value, datetime):
                row[key] = value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)
            elif hasattr(value, "isoformat"):
                row[key] = value.isoformat()
    return rows
