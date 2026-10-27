"""Download hourly Open-Meteo weather for one point (#8).

Each set in config/open_meteo.csv is one model from one Open-Meteo API, for
example ERA5 history or ECMWF IFS archived forecasts. Values are hourly and in
UTC; daily values and Boston-local days are built in #16. A set has its own
folder under data/raw/open_meteo/ with one JSON file per calendar year, byte
for byte as the API returned it, and nothing else, so DVC sees a change only
when a year file changes.

A set ends where its `end` rule says. `yesterday` stops at 23:00 UTC of the day
before the run, so no hour after the download is stored. `latest_values` stops
at the last full day the model has values for (ERA5 runs about 5 days behind),
found with one small request. A year already stored through its end is skipped
without a request. A `yesterday` year whose last day came back with gaps in a
value that has data stays marked incomplete and is fetched again: on every run
for FINAL_DAY_RETRY_DAYS after that day, then every FINAL_DAY_RECHECK_DAYS until
the gaps are filled. So a 31 December stored before Open-Meteo had all of it is
completed whenever Open-Meteo publishes the rest, and is never taken as complete
before that. Any other year is downloaded again whole, and the answer replaces
the file only after it passes its checks and only when its bytes changed, so a
rerun extends a set by the newly finished days, never duplicates an hour and
rewrites nothing that did not change.

A set's state lives under data/state/open_meteo/<set>/: _manifest.json (what is
stored, when, from which start date, the grid point Open-Meteo used, which
values have data) and _runs.jsonl (one line per run). A manifest that an
earlier version left in the raw folder is read there, never moved or changed
(see the runbook). docs/data/open-meteo.csv gets one row per file with the
CC BY 4.0 attribution, the data provider's credit, coverage, download time and
SHA-256. It is written only after a run with no failure, and only when its
content changes; a set the run did not cover (--only, or no manifest here)
keeps its rows.

HTTP 429 stops the run, since every further request would count against the
limit. A temporary server error (HTTP 5xx) is tried again after a short wait.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("open_meteo")

USER_AGENT = "boston-resilience-twin-weather/1.0 (Northeastern IE7374 course project)"
CONFIG_COLUMNS = [
    "name", "url", "model", "start", "end", "hourly", "place", "latitude", "longitude",
    "validation_test", "label", "credit", "credit_url",
]
END_RULES = {"latest_values", "yesterday"}
SET_NAME = re.compile(r"[a-z0-9][a-z0-9_]*")
API_NAME = re.compile(r"[a-z0-9_]+")
TIMEZONE = "GMT"  # stored in UTC; #16 turns hours into Boston-local days
WIND_SPEED_UNIT = "ms"  # m/s, the unit of the GHCN-D gusts
HOUR_FORMAT = "%Y-%m-%dT%H:%M"
# Up to 2 weeks and 10 values count as 1 API call, so the latest_values check costs 1.
PROBE_DAYS = 14
# A year of hourly values counts as about 26 calls; the free API allows 600 a minute.
PAUSE_SECONDS = 5.0
TIMEOUT_SECONDS = 120
# Open-Meteo answers with its nearest grid point (ERA5's is 0.11 degrees from Logan).
# One further away than this means a wrong place in the config.
MAX_GRID_DEGREES = 0.5
MANIFEST = "_manifest.json"
RUN_LOG = "_runs.jsonl"
# A `yesterday` year whose last day came back with gaps stays marked incomplete
# and is fetched again: on every run for this many days after that day, then every
# FINAL_DAY_RECHECK_DAYS, until the gaps are filled. It is never taken as complete.
FINAL_DAY_RETRY_DAYS = 7
FINAL_DAY_RECHECK_DAYS = 7
# A temporary server error (HTTP 5xx) is tried again after each of these waits.
SERVER_ERROR_WAITS = (10.0, 30.0)
YEAR_FILE = re.compile(r"(\d{4})\.json")
SHA256 = re.compile(r"[0-9a-f]{64}")
ENTRY_KEYS = ("first_hour", "last_hour", "hours", "values", "settings", "downloaded_at", "sha256")
FIX_MANIFEST = "Fix it, or delete it to download the set again."

LICENSE = "CC BY 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
ATTRIBUTION = "Weather data by Open-Meteo.com"
ATTRIBUTION_URL = "https://open-meteo.com/"
CHANGES = "none: API answers stored byte for byte (hourly, UTC, wind in m/s as requested); daily values come in #16"
ATTRIBUTION_COLUMNS = [
    "set", "file", "label", "validation_test", "api_url", "model", "place", "latitude", "longitude",
    "grid_latitude", "grid_longitude", "grid_elevation", "first_hour", "last_hour", "hours",
    "values_with_data", "values_without_data", "license", "license_url", "attribution",
    "attribution_url", "credit", "credit_url", "changes", "downloaded_at", "sha256",
]


@dataclass(frozen=True)
class WeatherSet:
    """One row of Data-Pipeline/config/open_meteo.csv: the API, model, hourly values, place and attribution of
    a set."""
    name: str
    url: str
    model: str
    start: date
    end: str
    hourly: tuple[str, ...]
    place: str
    latitude: float
    longitude: float
    validation_test: str
    label: str
    credit: str
    credit_url: str

    def settings(self) -> dict:
        """What every stored file depends on, apart from its dates."""
        return {
            "url": self.url,
            "model": self.model,
            "hourly": list(self.hourly),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


def read_config(path: Path | str, today: date | None = None) -> list[WeatherSet]:
    """Read and check the set list, so a bad edit fails before any request."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    if path.stat().st_size == 0:
        raise ValueError(f"{path.name} is empty")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(CONFIG_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path.name} lists no sets")

    today = today or datetime.now(timezone.utc).date()
    sets = []
    for line, row in enumerate(rows, start=2):
        # DictReader keeps fields beyond the header under the key None.
        if None in row:
            raise ValueError(f"{path.name} line {line} has more fields than the header")
        row = {k: (v or "").strip() for k, v in row.items()}
        sets.append(parse_set(row, f"{path.name} line {line}", today))
    names = [s.name for s in sets]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise ValueError(f"{path.name} repeats set names: {repeated}")
    return sets


def parse_set(row: dict, where: str, today: date) -> WeatherSet:
    """A config row as a WeatherSet. Raises ValueError, naming the row, when a field is not valid."""
    name = row["name"]
    if not SET_NAME.fullmatch(name):
        raise ValueError(f"{where}: set name {name!r} must be lowercase letters, digits and _, since it names the folder")
    url = urllib.parse.urlsplit(row["url"])
    host = url.hostname or ""
    if url.scheme != "https" or not (host == "open-meteo.com" or host.endswith(".open-meteo.com")) or url.query:
        raise ValueError(f"{where}: url {row['url']!r} must be an https Open-Meteo API address with no query")
    if not API_NAME.fullmatch(row["model"]):
        raise ValueError(f"{where}: model {row['model']!r} is not an Open-Meteo model name")
    try:
        start = date.fromisoformat(row["start"])
    except ValueError:
        raise ValueError(f"{where}: start {row['start']!r} is not a date like 2021-01-01") from None
    if start >= today:
        raise ValueError(f"{where}: start {start} is not before today ({today})")
    if row["end"] not in END_RULES:
        raise ValueError(f"{where}: end {row['end']!r}, expected one of {sorted(END_RULES)}")
    hourly = tuple(v.strip() for v in row["hourly"].split(";") if v.strip())
    if not hourly or not all(API_NAME.fullmatch(v) for v in hourly):
        raise ValueError(f"{where}: hourly needs value names separated by ';', got {row['hourly']!r}")
    if len(set(hourly)) != len(hourly):
        raise ValueError(f"{where}: hourly repeats a value: {row['hourly']!r}")
    try:
        latitude, longitude = float(row["latitude"]), float(row["longitude"])
    except ValueError:
        raise ValueError(f"{where}: latitude and longitude must be numbers") from None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError(f"{where}: latitude {latitude}, longitude {longitude} is not a place on Earth")
    if row["validation_test"] not in {"yes", "no"}:
        raise ValueError(f"{where}: validation_test must be yes or no, got {row['validation_test']!r}")
    for column in ("place", "label", "credit", "credit_url"):
        if not row[column]:
            raise ValueError(f"{where}: {column} is blank")
    return WeatherSet(
        name, row["url"], row["model"], start, row["end"], hourly, row["place"],
        latitude, longitude, row["validation_test"], row["label"], row["credit"], row["credit_url"],
    )


class ApiError(RuntimeError):
    """An Open-Meteo request failed with an HTTP status (`status`)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class RateLimited(ApiError):
    """HTTP 429: the run stops, since every further request would count against the limit."""


class OpenMeteoClient:
    """GET requests to the Open-Meteo APIs, at least `pause` seconds apart.

    HTTP 429 raises RateLimited at once. A temporary server error (HTTP 5xx) is
    tried again after each wait in `waits`, then raises ApiError. The status is
    kept even when the error answer's body cannot be read.
    """

    def __init__(self, pause: float = PAUSE_SECONDS, timeout: float = TIMEOUT_SECONDS,
                 waits: tuple[float, ...] = SERVER_ERROR_WAITS, sleep=time.sleep):
        self.pause = pause
        self.timeout = timeout
        self.waits = tuple(waits)
        self.sleep = sleep
        self._last = None

    def get(self, url: str) -> bytes:
        """GET url, trying a temporary server error (HTTP 5xx) again after each wait. Any other error is
        raised at once."""
        for attempt in range(len(self.waits) + 1):
            try:
                return self._get_once(url)
            except ApiError as exc:
                if not 500 <= exc.status < 600 or attempt == len(self.waits):
                    raise
                log.warning("%s; trying again in %.0f s", exc, self.waits[attempt])
                self.sleep(self.waits[attempt])
        raise AssertionError("unreachable")

    def _get_once(self, url: str) -> bytes:
        if self._last is not None:
            self.sleep(max(0.0, self.pause - (time.monotonic() - self._last)))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                reason = api_reason(exc.read())
            except Exception as read_error:  # for example a connection reset while reading the error body
                reason = f"the error body could not be read ({type(read_error).__name__})"
            error = RateLimited if exc.code == 429 else ApiError
            raise error(exc.code, f"HTTP {exc.code}: {reason}") from exc
        finally:
            self._last = time.monotonic()


def api_reason(body: bytes) -> str:
    """The `reason` of an Open-Meteo error answer, or the start of the body."""
    try:
        return str(json.loads(body)["reason"])
    except (ValueError, KeyError, TypeError):
        return body[:200].decode("utf-8", "replace")


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-year pieces of start to end, both ends included."""
    if end < start:
        return []
    return [(max(start, date(year, 1, 1)), min(end, date(year, 12, 31))) for year in range(start.year, end.year + 1)]


def hours(start: date, end: date) -> list[str]:
    """Every hour from start 00:00 to end 23:00, as the API writes them."""
    first = datetime(start.year, start.month, start.day)
    return [(first + timedelta(hours=i)).strftime(HOUR_FORMAT) for i in range(((end - start).days + 1) * 24)]


def request_url(ws: WeatherSet, start: date, end: date) -> str:
    """The API address for one set from start to end."""
    params = {
        "latitude": ws.latitude,
        "longitude": ws.longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(ws.hourly),
        "models": ws.model,
        "timezone": TIMEZONE,
        "wind_speed_unit": WIND_SPEED_UNIT,
    }
    return f"{ws.url}?{urllib.parse.urlencode(params, safe=',')}"


def check_answer(body: bytes, ws: WeatherSet, start: date, end: date) -> tuple[dict, dict]:
    """Return (hourly values, summary) of an API answer, or raise if it must not be stored.

    A value with no data at all (showers in ECMWF IFS) is kept and recorded,
    not treated as an error.
    """
    if not body.strip():
        raise ValueError("answer is empty")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"answer is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("answer is not a JSON object")
    if data.get("error"):
        raise ValueError(f"Open-Meteo refused the request: {data.get('reason')}")
    if data.get("utc_offset_seconds") != 0:
        raise ValueError(f"answer is not in UTC (utc_offset_seconds {data.get('utc_offset_seconds')!r})")
    lat, lon = data.get("latitude"), data.get("longitude")
    if not all(isinstance(x, (int, float)) for x in (lat, lon)):
        raise ValueError("answer has no grid point")
    if abs(lat - ws.latitude) > MAX_GRID_DEGREES or abs(lon - ws.longitude) > MAX_GRID_DEGREES:
        raise ValueError(
            f"answer's grid point ({lat}, {lon}) is more than {MAX_GRID_DEGREES} degrees "
            f"from the place in the config ({ws.latitude}, {ws.longitude})"
        )

    hourly = data.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ValueError("answer has no hourly time list")
    times, expected = hourly["time"], hours(start, end)
    if times != expected:
        got = f"{times[0]} to {times[-1]}, {len(times)} hours" if times else "no hours"
        raise ValueError(f"answer has {got}; expected {expected[0]} to {expected[-1]}, {len(expected)} hours")
    missing = [v for v in ws.hourly if v not in hourly]
    if missing:
        raise ValueError(f"answer is missing values {missing}")

    units = data.get("hourly_units") or {}
    found = {}
    for value in ws.hourly:
        series = hourly[value]
        if not isinstance(series, list) or len(series) != len(times):
            count = len(series) if isinstance(series, list) else "no"
            raise ValueError(f"{value} has {count} entries for {len(times)} hours")
        filled = [i for i, x in enumerate(series) if x is not None]
        if any(isinstance(series[i], bool) or not isinstance(series[i], (int, float)) for i in filled):
            raise ValueError(f"{value} has entries that are not numbers")
        found[value] = {
            "unit": units.get(value),
            "hours_with_data": len(filled),
            "first": times[filled[0]] if filled else None,
            "last": times[filled[-1]] if filled else None,
        }
    # The last day must have every hour of every value that has data at all; a value
    # the model lacks entirely (showers in ECMWF IFS before 2025) stays missing.
    gaps = [v for v in ws.hourly if found[v]["hours_with_data"] and any(x is None for x in hourly[v][-24:])]
    summary = {
        "first_hour": times[0],
        "last_hour": times[-1],
        "hours": len(times),
        "final_day_complete": not gaps,
        "final_day_gaps": gaps,
        "grid": {"latitude": lat, "longitude": lon, "elevation": data.get("elevation")},
        "values": found,
    }
    return hourly, summary


def latest_full_day(ws: WeatherSet, client, until: date) -> date | None:
    """Last day up to `until` with data in all 24 hours, from one small request.

    Values with no data anywhere in the window are left out of the test, so a
    value the model lacks does not hold the set back. None when no day is full.
    """
    start = max(ws.start, until - timedelta(days=PROBE_DAYS - 1))
    if start > until:
        return None
    hourly, _ = check_answer(client.get(request_url(ws, start, until)), ws, start, until)
    present = [v for v in ws.hourly if any(x is not None for x in hourly[v])]
    if not present:
        return None
    day = until
    while day >= start:
        first = (day - start).days * 24
        if all(x is not None for v in present for x in hourly[v][first:first + 24]):
            return day
        day -= timedelta(days=1)
    return None


def set_end(ws: WeatherSet, client, now: datetime) -> date:
    """Last day to store: yesterday in UTC, or the last full day the model has values for."""
    yesterday = now.astimezone(timezone.utc).date() - timedelta(days=1)
    if ws.end == "yesterday":
        return yesterday
    end = latest_full_day(ws, client, yesterday)
    if end is None:
        raise ValueError(f"no full day with values in the {PROBE_DAYS} days up to {yesterday}")
    return end


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, body: bytes) -> None:
    """Write body through a hidden temporary file and rename it, so a crash never leaves half a file."""
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: dict) -> None:
    """Write data as sorted, indented JSON (write_atomic)."""
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True, default=str).encode())


def load_manifest(state_folder: Path, legacy_folder: Path | None = None) -> dict:
    """A set's manifest from its state folder, checked entry by entry.

    A manifest that an earlier version kept in the raw folder is read there when
    the state folder has none yet, never moved or changed (see the runbook).
    """
    path = Path(state_folder) / MANIFEST
    if not path.exists() and legacy_folder is not None and (Path(legacy_folder) / MANIFEST).exists():
        path = Path(legacy_folder) / MANIFEST
        log.warning("no manifest in %s yet; reading %s (move it there, see the runbook)", state_folder, path)
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}). {FIX_MANIFEST}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"{path} is not a JSON object. {FIX_MANIFEST}")
    manifest.setdefault("files", {})
    check_manifest(manifest, path)
    return manifest


def save_manifest(state_folder: Path, manifest: dict) -> None:
    """Write a set's manifest to its state folder (write_json_atomic)."""
    Path(state_folder).mkdir(parents=True, exist_ok=True)
    write_json_atomic(Path(state_folder) / MANIFEST, manifest)


def check_manifest(manifest: dict, path: Path) -> None:
    """Raise a clear ValueError for a manifest the run must not trust."""
    if not isinstance(manifest["files"], dict):
        raise ValueError(f"{path}: 'files' is not an object. {FIX_MANIFEST}")
    start = manifest.get("start")
    if start is not None:
        try:
            date.fromisoformat(start)
        except (TypeError, ValueError):
            raise ValueError(f"{path}: start {start!r} is not a date. {FIX_MANIFEST}") from None
    for name, entry in manifest["files"].items():
        problem = entry_problem(name, entry)
        if problem:
            raise ValueError(f"{path}: entry {name!r} {problem}. Fix the entry, or delete it to download that year again.")


def entry_problem(name: str, entry) -> str | None:
    """What is wrong with one manifest entry, or None."""
    if not isinstance(entry, dict):
        return "is not an object"
    year = YEAR_FILE.fullmatch(name)
    if not year:
        return "is not a year file name like 2025.json"
    missing = [k for k in ENTRY_KEYS if k not in entry]
    if missing:
        return f"lacks {missing}"
    try:
        first = datetime.strptime(entry["first_hour"], HOUR_FORMAT)
        last = datetime.strptime(entry["last_hour"], HOUR_FORMAT)
        datetime.fromisoformat(entry["downloaded_at"])
    except (TypeError, ValueError):
        return "has a first_hour, last_hour or downloaded_at that is not a time"
    if first.year != int(year.group(1)) or last.year != first.year or last < first:
        return f"covers {entry['first_hour']} to {entry['last_hour']}, not one span inside {year.group(1)}"
    count = entry["hours"]
    if isinstance(count, bool) or not isinstance(count, int) or count != (last - first) // timedelta(hours=1) + 1:
        return f"has hours {count!r}, which does not match {entry['first_hour']} to {entry['last_hour']}"
    if not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"]):
        return "has no valid sha256"
    values = entry["values"]
    if not isinstance(values, dict) or not all(isinstance(v, dict) and isinstance(v.get("hours_with_data"), int) for v in values.values()):
        return "has values without hours_with_data"
    if not isinstance(entry["settings"], dict):
        return "has settings that are not an object"
    checked = entry.get("final_day_checked")
    if checked is not None:
        try:
            date.fromisoformat(checked)
        except (TypeError, ValueError):
            return f"has final_day_checked {checked!r}, which is not a date"
    return None


def final_day_complete(entry: dict) -> bool:
    """Whether a stored file's last day has every hour of every value that has data.

    Entries written before this check have no flag; for them every value with
    data must reach the file's last hour.
    """
    if "final_day_complete" in entry:
        return bool(entry["final_day_complete"])
    return all(v.get("last") == entry["last_hour"] for v in entry["values"].values() if v.get("hours_with_data"))


def final_day_due(ws: WeatherSet, entry: dict, now: datetime) -> bool:
    """True when a `yesterday` file's incomplete last day should be fetched again now:
    on every run for FINAL_DAY_RETRY_DAYS after that day, then every
    FINAL_DAY_RECHECK_DAYS after the last check. It is never taken as complete."""
    if ws.end != "yesterday" or final_day_complete(entry):
        return False
    today = now.astimezone(timezone.utc).date()
    if today <= date.fromisoformat(entry["last_hour"][:10]) + timedelta(days=FINAL_DAY_RETRY_DAYS):
        return True
    return today >= next_final_day_check(entry)


def next_final_day_check(entry: dict) -> date:
    """When an incomplete last day is fetched again, once its daily retries are over.
    Entries from before the check date was recorded use their download date."""
    checked = entry.get("final_day_checked") or entry["downloaded_at"][:10]
    return date.fromisoformat(checked) + timedelta(days=FINAL_DAY_RECHECK_DAYS)


def stored_start(manifest: dict) -> date | None:
    """The start date the stored files were downloaded with. Manifests from before
    the start check have no `start`; their earliest first hour stands in."""
    if manifest.get("start"):
        return date.fromisoformat(manifest["start"])
    firsts = [entry["first_hour"][:10] for entry in manifest["files"].values()]
    return date.fromisoformat(min(firsts)) if firsts else None


def years_before_start(ws: WeatherSet, manifest: dict, force: bool) -> list[str]:
    """Stored years that a later start in the config leaves outside the set.

    Without --force a later start stops the set, so stored hours that no longer
    match the config are never kept as if they were current; with --force those
    years are dropped. An earlier start just downloads the new years.
    """
    previous = stored_start(manifest)
    if previous is None or ws.start <= previous:
        return []
    if not force:
        raise ValueError(
            f"start moved from {previous} to {ws.start}: the stored hours before {ws.start} no longer match "
            "the config; give the set a new name, or rerun with --force to drop them"
        )
    return sorted(name for name in manifest["files"] if int(name[:4]) < ws.start.year)


def refresh_file(ws, folder, manifest, client, start, stop, now, force=False, dry_run=False) -> dict:
    """Download one calendar year of a set, unless it is already stored through `stop`."""
    name = f"{start.year}.json"
    target = folder / name
    state = manifest["files"].get(name, {})
    first, last = f"{start.isoformat()}T00:00", f"{stop.isoformat()}T23:00"
    today = now.astimezone(timezone.utc).date().isoformat()
    result = {"file": name, "action": "skipped", "reason": "", "hours": state.get("hours"), "last_hour": state.get("last_hour")}
    stored = (
        target.exists()
        and state.get("settings") == ws.settings()
        and state.get("first_hour") == first
        and (state.get("last_hour") or "") >= last
        and not final_day_due(ws, state, now)
        and state.get("sha256") == file_sha256(target)
    )
    if stored and not force:
        result["reason"] = f"stored through {state['last_hour']}"
        if ws.end == "yesterday" and not final_day_complete(state):
            gaps = state.get("final_day_gaps") or "some values"
            result["reason"] += f"; last day still has gaps in {gaps}, next check on or after {next_final_day_check(state)}"
        return result
    if dry_run:
        result.update(action="would_download", reason=f"{first} to {last}")
        return result

    url = request_url(ws, start, stop)
    body = client.get(url)
    _, summary = check_answer(body, ws, start, stop)
    digest = hashlib.sha256(body).hexdigest()
    result.update(reason=f"{first} to {last}", hours=summary["hours"], last_hour=summary["last_hour"])
    if not summary["final_day_complete"]:
        result["reason"] += f"; last day has gaps in {summary['final_day_gaps']}, checked again until they are filled"
    existed = target.exists()
    same_bytes = existed and file_sha256(target) == digest
    if same_bytes and state.get("sha256") == digest and state.get("settings") == ws.settings() and state.get("url") == url:
        # Same bytes from the same request: the file stays as it is, and so does its
        # entry, apart from the date an incomplete last day was last checked.
        result["action"] = "unchanged"
        if not summary["final_day_complete"]:
            state["final_day_checked"] = today
            result["checked"] = True
        return result
    if not same_bytes:
        write_atomic(target, body)
    manifest["files"][name] = {
        **summary,
        "settings": ws.settings(),
        "url": url,
        "downloaded_at": now.isoformat(timespec="seconds"),
        "bytes": len(body),
        "sha256": digest,
    }
    if not summary["final_day_complete"]:
        manifest["files"][name]["final_day_checked"] = today
    result["action"] = "updated" if existed else "downloaded"
    return result


def refresh_set(ws: WeatherSet, raw_root: Path, state_root: Path, client, now: datetime, force=False, dry_run=False) -> dict:
    """Bring one set up to its end. Returns its record for _runs.jsonl."""
    folder, state = Path(raw_root) / ws.name, Path(state_root) / ws.name
    manifest = load_manifest(state, folder)
    stored = manifest.get("settings")
    if stored is not None and stored != ws.settings() and not force:
        raise ValueError(
            f"its files were downloaded with other settings ({stored}); give the set a new name "
            "in the config, or rerun with --force to replace them"
        )
    dropped = years_before_start(ws, manifest, force)
    end = set_end(ws, client, now)
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob(".*.part"):
            stale.unlink()
        manifest.update(set=ws.name, settings=ws.settings(), start=ws.start.isoformat())
        for name in dropped:
            log.warning("%s %s: before the new start %s, dropped", ws.name, name, ws.start)
            del manifest["files"][name]
            (folder / name).unlink(missing_ok=True)
        if dropped:
            save_manifest(state, manifest)

    results, stopped = [], None
    chunks = year_chunks(ws.start, end)
    for i, (start, stop) in enumerate(chunks):
        try:
            result = refresh_file(ws, folder, manifest, client, start, stop, now, force, dry_run)
        except RateLimited as exc:
            log.error("%s %s: %s; the run stops here", ws.name, start.year, exc)
            results.append({"file": f"{start.year}.json", "action": "failed", "reason": f"{type(exc).__name__}: {exc}"})
            results += [{"file": f"{s.year}.json", "action": "not_tried", "reason": "the run stopped after HTTP 429"}
                        for s, _ in chunks[i + 1:]]
            stopped = str(exc)
            break
        except Exception as exc:
            log.error("%s %s failed, stored file kept: %s", ws.name, start.year, exc)
            result = {"file": f"{start.year}.json", "action": "failed", "reason": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        log.info("%s %s %s (%s)", ws.name, result["file"], result["action"], result["reason"])
        if result["action"] in ("downloaded", "updated"):
            files = manifest["files"].values()
            manifest["first_hour"] = min(f["first_hour"] for f in files)
            manifest["last_hour"] = max(f["last_hour"] for f in files)
            manifest["downloaded_at"] = now.isoformat(timespec="seconds")
            save_manifest(state, manifest)
        elif result.get("checked"):
            save_manifest(state, manifest)  # only the date an incomplete last day was checked changed
    failed = [f"{ws.name}/{r['file']}" for r in results if r["action"] == "failed"]
    record = {"set": ws.name, "end": end.isoformat(), "files": results, "failed": failed}
    if dropped:
        record["dropped"] = dropped
    if stopped:
        record["stopped"] = stopped
    return record


def run_download(sets, raw_root, client, *, state_root, now=None, only=None, force=False, dry_run=False) -> dict:
    """One run over the sets. run["failed"] names every set or file that failed, and
    run["stopped"] says why the run stopped early (HTTP 429)."""
    now = now or datetime.now(timezone.utc)
    if only:
        unknown = sorted(set(only) - {s.name for s in sets})
        if unknown:
            raise ValueError(f"unknown set(s) {unknown}; known: {[s.name for s in sets]}")
        sets = [s for s in sets if s.name in only]

    runs, stopped = [], None
    for ws in sets:
        if stopped:
            record = {"set": ws.name, "end": None, "files": [], "failed": [ws.name],
                      "reason": f"not tried: the run stopped after {stopped}"}
        else:
            try:
                record = refresh_set(ws, raw_root, state_root, client, now, force, dry_run)
            except RateLimited as exc:
                log.error("%s: %s; the run stops here", ws.name, exc)
                record = {"set": ws.name, "end": None, "files": [], "failed": [ws.name],
                          "reason": f"{type(exc).__name__}: {exc}", "stopped": str(exc)}
            except Exception as exc:
                log.error("%s failed, stored files kept: %s", ws.name, exc)
                record = {"set": ws.name, "end": None, "files": [], "failed": [ws.name], "reason": f"{type(exc).__name__}: {exc}"}
            stopped = record.get("stopped")
        record = {"run_at": now.isoformat(timespec="seconds"), "dry_run": dry_run, **record}
        if not dry_run:
            state = Path(state_root) / ws.name
            state.mkdir(parents=True, exist_ok=True)
            with open(state / RUN_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        runs.append(record)
    run = {"run_at": now.isoformat(timespec="seconds"), "sets": runs, "failed": [f for r in runs for f in r["failed"]]}
    if stopped:
        run["stopped"] = stopped
    return run


def describe(value: str, found: dict, hours: int) -> str:
    """`value (unit)`, plus its hours and first and last hour when it does not fill the file."""
    if found["hours_with_data"] == hours:
        return f"{value} ({found['unit']})"
    return f"{value} ({found['unit']}, {found['hours_with_data']} of {hours} hours, {found['first']} to {found['last']})"


def read_attribution(path: Path) -> list[dict]:
    """The rows already in the attribution file, or none when it does not exist yet."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ATTRIBUTION_COLUMNS:
            raise ValueError(f"{path} does not have the attribution columns; fix it before a run writes it again")
        return list(reader)


def attribution_rows(sets, state_root, existing=(), only=None, raw_root=None) -> list[dict]:
    """One row per stored file: what it is, where it came from, and how to credit it.

    A set outside `only`, or one with no stored files here (a teammate who has
    only some sets), keeps its rows from `existing` as they are, so no run drops
    another set's credit.
    """
    rows = []
    for ws in sets:
        previous = [row for row in existing if row["set"] == ws.name]
        if only and ws.name not in only:
            rows += previous
            continue
        legacy = Path(raw_root) / ws.name if raw_root is not None else None
        files = load_manifest(Path(state_root) / ws.name, legacy)["files"]
        if not files:
            if previous:
                log.warning("%s: no stored files here; its %d rows in the attribution file are kept", ws.name, len(previous))
            rows += previous
            continue
        for name in sorted(files):
            entry = files[name]
            values = entry.get("values", {})
            with_data = [describe(v, values[v], entry["hours"]) for v in ws.hourly if values.get(v, {}).get("hours_with_data")]
            without_data = [v for v in ws.hourly if not values.get(v, {}).get("hours_with_data")]
            grid = entry.get("grid", {})
            rows.append({
                "set": ws.name,
                "file": f"{ws.name}/{name}",
                "label": ws.label,
                "validation_test": ws.validation_test,
                "api_url": ws.url,
                "model": ws.model,
                "place": ws.place,
                "latitude": ws.latitude,
                "longitude": ws.longitude,
                "grid_latitude": grid.get("latitude"),
                "grid_longitude": grid.get("longitude"),
                "grid_elevation": grid.get("elevation"),
                "first_hour": entry["first_hour"],
                "last_hour": entry["last_hour"],
                "hours": entry["hours"],
                "values_with_data": "; ".join(with_data),
                "values_without_data": "; ".join(without_data),
                "license": LICENSE,
                "license_url": LICENSE_URL,
                "attribution": ATTRIBUTION,
                "attribution_url": ATTRIBUTION_URL,
                "credit": ws.credit.replace("{year}", entry["downloaded_at"][:4]),
                "credit_url": ws.credit_url,
                "changes": CHANGES,
                "downloaded_at": entry["downloaded_at"],
                "sha256": entry["sha256"],
            })
    return rows


def write_attribution(path: Path, rows: list[dict]) -> bool:
    """Write the attribution CSV when its content changes. Returns whether it was written."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=ATTRIBUTION_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    body = buffer.getvalue().encode("utf-8")
    path = Path(path)
    if path.exists() and path.read_bytes() == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, body)
    return True


def main(argv: list[str] | None = None) -> None:
    """Download each configured set up to its last finished day and, after a run with no failure, write the
    attribution rows; --dry-run only sends the ERA5 end check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="Data-Pipeline/config/open_meteo.csv")
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/open_meteo", help="the year files, and nothing else")
    parser.add_argument("--state-dir", default="Data-Pipeline/data/state/open_meteo", help="manifests and run logs")
    parser.add_argument("--attribution", default="docs/data/open-meteo.csv", help="CSV with one row per stored file")
    parser.add_argument("--only", nargs="+", metavar="SET", help="limit the run to these sets")
    parser.add_argument("--force", action="store_true", help="download every year again, even if stored")
    parser.add_argument("--dry-run", action="store_true", help="print what a run would download; writes nothing")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS, help="seconds between requests")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    sets = read_config(args.config)
    raw_dir, state_dir, attribution = Path(args.raw_dir), Path(args.state_dir), Path(args.attribution)
    run = run_download(
        sets, raw_dir, OpenMeteoClient(pause=args.pause), state_root=state_dir,
        only=args.only, force=args.force, dry_run=args.dry_run,
    )
    for record in run["sets"]:
        if record["end"] is None:
            print(f"{record['set']}: FAILED, {record['reason']}")
            continue
        print(f"{record['set']}: up to {record['end']} 23:00 UTC")
        for f in record["files"]:
            print(f"  {f['file']} {f['action']}: {f['reason']} (hours {f.get('hours')}, last hour {f.get('last_hour')})")
    if run.get("stopped"):
        print(f"STOPPED after {run['stopped']}: wait about an hour and rerun; finished years are not downloaded again")
    if args.dry_run:
        pass
    elif run["failed"]:
        print(f"{attribution} not written: the run had failures, so it keeps its rows")
    else:
        try:
            rows = attribution_rows(sets, state_dir, read_attribution(attribution), only=args.only, raw_root=raw_dir)
        except ValueError as exc:
            print(f"{attribution} not written: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        if write_attribution(attribution, rows):
            print(f"wrote {len(rows)} rows to {attribution}")
        else:
            print(f"{attribution} unchanged ({len(rows)} rows)")
    if run["failed"]:
        print(f"FAILED: {', '.join(run['failed'])}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
