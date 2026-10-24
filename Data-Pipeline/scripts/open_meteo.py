"""Download hourly Open-Meteo weather for one point (#8).

Each set in config/open_meteo.csv is one model from one Open-Meteo API, for
example ERA5 history or ECMWF IFS archived forecasts. Values are hourly and in
UTC; daily values and Boston-local days are built in #16. A set has its own
folder under data/raw/open_meteo/, with one JSON file per calendar year, byte
for byte as the API returned it.

A set ends where its `end` rule says. `yesterday` stops at 23:00 UTC of the day
before the run, so no hour after the download is stored. `latest_values` stops
at the last full day the model has values for (ERA5 runs about 5 days behind),
found with one small request. A year already stored through its end is skipped
without a request. Any other year is downloaded again whole, and the answer
replaces the file only after it passes its checks, so a rerun extends a set by
the newly finished days and never duplicates an hour.

Each set folder has _manifest.json (what is stored, when, the grid point
Open-Meteo used, which values have data) and _runs.jsonl (one line per run).
docs/data/open-meteo.csv gets one row per file with the CC BY 4.0 attribution,
the data provider's credit, coverage, download time and SHA-256.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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


class OpenMeteoClient:
    """GET requests to the Open-Meteo APIs, at least `pause` seconds apart."""

    def __init__(self, pause: float = PAUSE_SECONDS, timeout: float = TIMEOUT_SECONDS):
        self.pause = pause
        self.timeout = timeout
        self._last = None

    def get(self, url: str) -> bytes:
        if self._last is not None:
            time.sleep(max(0.0, self.pause - (time.monotonic() - self._last)))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {api_reason(exc.read())}") from exc
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
    summary = {
        "first_hour": times[0],
        "last_hour": times[-1],
        "hours": len(times),
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
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, body: bytes) -> None:
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: dict) -> None:
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True, default=str).encode())


def load_manifest(folder: Path) -> dict:
    path = folder / MANIFEST
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}). Fix it, or delete it to download the set again.") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"{path} is not a JSON object. Fix it, or delete it to download the set again.")
    manifest.setdefault("files", {})
    return manifest


def refresh_file(ws, folder, manifest, client, start, stop, now, force=False, dry_run=False) -> dict:
    """Download one calendar year of a set, unless it is already stored through `stop`."""
    name = f"{start.year}.json"
    target = folder / name
    state = manifest["files"].get(name, {})
    first, last = f"{start.isoformat()}T00:00", f"{stop.isoformat()}T23:00"
    result = {"file": name, "action": "skipped", "reason": "", "hours": state.get("hours"), "last_hour": state.get("last_hour")}
    stored = (
        target.exists()
        and state.get("settings") == ws.settings()
        and state.get("first_hour") == first
        and (state.get("last_hour") or "") >= last
        and state.get("sha256") == file_sha256(target)
    )
    if stored and not force:
        result["reason"] = f"stored through {state['last_hour']}"
        return result
    if dry_run:
        result.update(action="would_download", reason=f"{first} to {last}")
        return result

    url = request_url(ws, start, stop)
    body = client.get(url)
    _, summary = check_answer(body, ws, start, stop)
    existed = target.exists()
    write_atomic(target, body)
    manifest["files"][name] = {
        **summary,
        "settings": ws.settings(),
        "url": url,
        "downloaded_at": now.isoformat(timespec="seconds"),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    result.update(
        action="updated" if existed else "downloaded",
        reason=f"{first} to {last}",
        hours=summary["hours"],
        last_hour=summary["last_hour"],
    )
    return result


def refresh_set(ws: WeatherSet, raw_root: Path, client, now: datetime, force=False, dry_run=False) -> dict:
    """Bring one set up to its end. Returns its record for _runs.jsonl."""
    folder = Path(raw_root) / ws.name
    manifest = load_manifest(folder)
    stored = manifest.get("settings")
    if stored is not None and stored != ws.settings() and not force:
        raise ValueError(
            f"its files were downloaded with other settings ({stored}); give the set a new name "
            "in the config, or rerun with --force to replace them"
        )
    end = set_end(ws, client, now)
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob(".*.part"):
            stale.unlink()
        manifest.update(set=ws.name, settings=ws.settings())

    results = []
    for start, stop in year_chunks(ws.start, end):
        try:
            result = refresh_file(ws, folder, manifest, client, start, stop, now, force, dry_run)
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
            write_json_atomic(folder / MANIFEST, manifest)
    failed = [f"{ws.name}/{r['file']}" for r in results if r["action"] == "failed"]
    return {"set": ws.name, "end": end.isoformat(), "files": results, "failed": failed}


def run_download(sets, raw_root, client, now=None, only=None, force=False, dry_run=False) -> dict:
    """One run over the sets. run["failed"] names every set or file that failed."""
    now = now or datetime.now(timezone.utc)
    if only:
        unknown = sorted(set(only) - {s.name for s in sets})
        if unknown:
            raise ValueError(f"unknown set(s) {unknown}; known: {[s.name for s in sets]}")
        sets = [s for s in sets if s.name in only]

    runs = []
    for ws in sets:
        try:
            record = refresh_set(ws, raw_root, client, now, force, dry_run)
        except Exception as exc:
            log.error("%s failed, stored files kept: %s", ws.name, exc)
            record = {"set": ws.name, "end": None, "files": [], "failed": [ws.name], "reason": f"{type(exc).__name__}: {exc}"}
        record = {"run_at": now.isoformat(timespec="seconds"), "dry_run": dry_run, **record}
        if not dry_run:
            folder = Path(raw_root) / ws.name
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / RUN_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        runs.append(record)
    return {"run_at": now.isoformat(timespec="seconds"), "sets": runs, "failed": [f for r in runs for f in r["failed"]]}


def describe(value: str, found: dict, hours: int) -> str:
    """`value (unit)`, plus its hours and first and last hour when it does not fill the file."""
    if found["hours_with_data"] == hours:
        return f"{value} ({found['unit']})"
    return f"{value} ({found['unit']}, {found['hours_with_data']} of {hours} hours, {found['first']} to {found['last']})"


def attribution_rows(sets, raw_root) -> list[dict]:
    """One row per stored file: what it is, where it came from, and how to credit it."""
    rows = []
    for ws in sets:
        try:
            files = load_manifest(Path(raw_root) / ws.name)["files"]
        except ValueError as exc:
            log.warning("%s left out of the attribution file: %s", ws.name, exc)
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


def write_attribution(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ATTRIBUTION_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="Data-Pipeline/config/open_meteo.csv")
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/open_meteo")
    parser.add_argument("--attribution", default="docs/data/open-meteo.csv", help="CSV with one row per stored file")
    parser.add_argument("--only", nargs="+", metavar="SET", help="limit the run to these sets")
    parser.add_argument("--force", action="store_true", help="download every year again, even if stored")
    parser.add_argument("--dry-run", action="store_true", help="print what a run would download; writes nothing")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS, help="seconds between requests")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    sets = read_config(args.config)
    run = run_download(
        sets, Path(args.raw_dir), OpenMeteoClient(pause=args.pause),
        only=args.only, force=args.force, dry_run=args.dry_run,
    )
    for record in run["sets"]:
        if record["end"] is None:
            print(f"{record['set']}: FAILED, {record['reason']}")
            continue
        print(f"{record['set']}: up to {record['end']} 23:00 UTC")
        for f in record["files"]:
            print(f"  {f['file']} {f['action']}: {f['reason']} (hours {f.get('hours')}, last hour {f.get('last_hour')})")
    if not args.dry_run:
        rows = attribution_rows(sets, Path(args.raw_dir))
        write_attribution(Path(args.attribution), rows)
        print(f"wrote {len(rows)} rows to {args.attribution}")
    if run["failed"]:
        print(f"FAILED: {', '.join(run['failed'])}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
