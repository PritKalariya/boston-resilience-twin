"""Shared helpers for the weather and tide downloads (#7).

Each download keeps raw files exactly as the source returned them, one file per
period, in its own folder under Data-Pipeline/data/raw/; that folder holds
nothing else. The bookkeeping lives apart, in the source's folder under
Data-Pipeline/data/state/: its _manifest.json records, per file, the period it
covers, when it was downloaded and its SHA-256. The manifest changes every run
(the download time), so keeping it out of raw/ means a run with nothing new
leaves raw/ byte for byte as it was, and DVC records no new version. A period counts as final once it was downloaded a set number
of days after it ended (sources revise recent data), and a final file is never
downloaded again. Any other period is downloaded whole and replaces its file
only after the answer passes the caller's checks, so a rerun adds the newly
finished days and never duplicates or half-writes a file. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MANIFEST = "_manifest.json"
TIMEOUT_SECONDS = 60


class FetchError(RuntimeError):
    """A request failed or its answer must not be stored."""


def get(url: str, headers: dict[str, str], timeout: float = TIMEOUT_SECONDS) -> bytes:
    """GET a URL and return the body. HTTP and network errors become FetchError
    with the status and the source's own message, never the request headers."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise FetchError(f"HTTP {e.code} {e.reason}: {detail}".rstrip(": ")) from None
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"could not reach {urllib.request.Request(url).host}: {getattr(e, 'reason', e)}") from None


def parse_json(body: bytes, what: str):
    """The JSON in an answer. FetchError, naming what, when it is empty or not valid JSON."""
    if not body.strip():
        raise FetchError(f"{what}: the answer is empty")
    try:
        return json.loads(body)
    except ValueError as e:
        raise FetchError(f"{what}: the answer is not valid JSON ({e})") from None


def write_atomic(path: Path, body: bytes) -> None:
    """Write next to the target, then replace it, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.part")
    partial.write_bytes(body)
    os.replace(partial, path)


def sha256(body: bytes) -> str:
    """SHA-256 of bytes, as hex."""
    return hashlib.sha256(body).hexdigest()


def load_manifest(state_dir: Path) -> dict:
    """The manifest in state_dir ({} when there is none). FetchError saying how to fix it when it is broken."""
    path = state_dir / MANIFEST
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text())
    except ValueError as e:
        raise FetchError(f"{path} is not valid JSON ({e}); fix it, or delete it to download the folder again") from None
    if not isinstance(manifest, dict):
        raise FetchError(f"{path} is not a JSON object; fix it, or delete it to download the folder again")
    return manifest


def save_manifest(state_dir: Path, manifest: dict) -> None:
    """Write the manifest to state_dir, sorted (write_atomic)."""
    write_atomic(state_dir / MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


@dataclass(frozen=True)
class Period:
    """A file's span, both ends inclusive."""

    name: str
    start: date
    end: date


def years(first: date, last: date) -> list[Period]:
    """One Period per calendar year from first to last, the first and last years cut to those days."""
    return [
        Period(str(y), max(first, date(y, 1, 1)), min(last, date(y, 12, 31)))
        for y in range(first.year, last.year + 1)
    ]


def half_years(first: date, last: date) -> list[Period]:
    """Halves of each year: a source with a 365-day limit cannot take a leap year whole."""
    periods = []
    for y in range(first.year, last.year + 1):
        for half, (a, b) in (("H1", (date(y, 1, 1), date(y, 6, 30))), ("H2", (date(y, 7, 1), date(y, 12, 31)))):
            start, end = max(first, a), min(last, b)
            if start <= end:
                periods.append(Period(f"{y}-{half}", start, end))
    return periods


def is_final(entry: dict | None, period: Period, settle_days: int) -> bool:
    """True when the stored file covers the whole period and was downloaded at
    least settle_days after it ended, so the source will not change it again."""
    if not entry or entry.get("end") != period.end.isoformat() or entry.get("start") != period.start.isoformat():
        return False
    downloaded = datetime.fromisoformat(entry["downloaded_at"]).date()
    return downloaded >= period.end + timedelta(days=settle_days)


def store(folder: Path, state_dir: Path, manifest: dict, filename: str, period: Period,
          body: bytes, now: datetime, **extra) -> str:
    """Write a checked answer to folder (only when it changed) and record it in
    state_dir's manifest. Returns "unchanged", "updated" or "downloaded"."""
    target = folder / filename
    entry = {"start": period.start.isoformat(), "end": period.end.isoformat(),
             "downloaded_at": now.isoformat(timespec="seconds"), "bytes": len(body), "sha256": sha256(body), **extra}
    if target.exists() and manifest.get(filename, {}).get("sha256") == entry["sha256"]:
        action = "unchanged"
    else:
        action = "updated" if target.exists() else "downloaded"
        write_atomic(target, body)
    manifest[filename] = entry
    save_manifest(state_dir, manifest)
    return action


def utc_now() -> datetime:
    """The current time in UTC; tests replace it."""
    return datetime.now(timezone.utc)
