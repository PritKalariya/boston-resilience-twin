"""Download the Boston 311 files from Analyze Boston on a refresh schedule (#6).

Each source in config/ingest_311_sources.csv has its own refresh interval. A
source that is not due is skipped without any request. A due source with
catalog change detection is downloaded only when its catalog hash, size, or
last-modified time changed; a source without it is downloaded every time it is
due. A download replaces the current file only after it passes its checks, and
files are replaced whole, so a rerun never appends or duplicates records.

The raw folder holds only the downloads, byte for byte as published
(<year>.csv, new_system.csv), and changes only when a source changed, so it is
the folder DVC versions. The schedule state (_manifest.json) and the run log
(_runs.jsonl, one line per run with every file's action and row count) live in
the state folder. New-system records are also written to the interim folder as
new_system_as_published.parquet: every value as published text, so the
timestamps stay untouched (Boston local time despite the +00, ADR-020), plus a
source_system column set to "new", the label map_311 uses. The script writes
these local folders only; raw data reaches Cloud Storage through DVC.

In the daily_pipeline DAG, ingest_legacy_311 runs with --system legacy and
then ingest_new_311 with --system new_system, one after the other, so the two
never write the state file at the same time. A failed source makes the run
exit with code 1, which fails the task.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from legacy_311 import REQUIRED_COLUMNS as LEGACY_REQUIRED_COLUMNS
from legacy_311 import scan_records
from new_311 import REQUIRED_COLUMNS as NEW_REQUIRED_COLUMNS

log = logging.getLogger("ingest_311")

CKAN_BASE_URL = "https://data.boston.gov"
USER_AGENT = "boston-resilience-twin-ingest/1.0 (Northeastern IE7374 course project)"
NEW_SOURCE_SYSTEM = "new"  # the label map_311 gives new-system rows in the combined table
SOURCE_COLUMNS = ["source", "resource_id", "filename", "system", "refresh_days", "change_detection"]
SYSTEMS = {"legacy", "new_system"}
CHANGE_DETECTION = {"catalog", "none"}
CATALOG_FIELDS = ("hash", "last_modified", "size")
MANIFEST = "_manifest.json"
RUN_LOG = "_runs.jsonl"
PREPARED_SUFFIX = "_as_published.parquet"
CHUNK_BYTES = 1 << 20
TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Source:
    source: str
    resource_id: str
    filename: str
    system: str
    refresh_days: int
    change_detection: str

    @property
    def required_columns(self) -> set[str]:
        if self.system == "legacy":
            return set(LEGACY_REQUIRED_COLUMNS)
        return set(NEW_REQUIRED_COLUMNS)

    @property
    def prepared_name(self) -> str | None:
        """File name of the as-published parquet in the interim folder (new-system sources only)."""
        if self.system != "new_system":
            return None
        return f"{Path(self.filename).stem}{PREPARED_SUFFIX}"


def load_sources(path: Path | str) -> list[Source]:
    """Read and check the source list, so a bad edit fails before anything is downloaded."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(SOURCE_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")
        rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]
    if not rows:
        raise ValueError(f"{path.name} lists no sources")

    sources = []
    for row in rows:
        name = row["source"] or "<blank>"
        if not row["source"] or not row["resource_id"]:
            raise ValueError(f"{path.name}: source {name} needs a name and a resource_id")
        filename = row["filename"]
        if Path(filename).name != filename or not filename.endswith(".csv") or filename[:1] in {".", "_"}:
            raise ValueError(f"{path.name}: source {name} has a bad filename {filename!r}")
        if row["system"] not in SYSTEMS:
            raise ValueError(f"{path.name}: source {name} has system {row['system']!r}, expected one of {sorted(SYSTEMS)}")
        if row["change_detection"] not in CHANGE_DETECTION:
            raise ValueError(
                f"{path.name}: source {name} has change_detection {row['change_detection']!r}, "
                f"expected one of {sorted(CHANGE_DETECTION)}"
            )
        try:
            refresh_days = int(row["refresh_days"])
        except ValueError:
            refresh_days = 0
        if refresh_days < 1:
            raise ValueError(f"{path.name}: source {name} needs refresh_days of 1 or more, got {row['refresh_days']!r}")
        sources.append(Source(row["source"], row["resource_id"], filename, row["system"], refresh_days, row["change_detection"]))

    for field in ("source", "filename"):
        values = [getattr(s, field) for s in sources]
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise ValueError(f"{path.name} repeats {field}: {dupes}")
    return sources


class StripDefaultPortRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects with a default port (:443, :80) removed from the new URL.

    Analyze Boston redirects file downloads to presigned S3 URLs written as
    s3.amazonaws.com:443. urllib copies that port into the Host header, and S3
    then rejects the signature with 403 SignatureDoesNotMatch; curl drops it.
    """

    DEFAULT_PORTS = {"http": 80, "https": 443}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.port is not None and parts.port == self.DEFAULT_PORTS.get(parts.scheme):
            newurl = parts._replace(netloc=parts.netloc.rsplit(":", 1)[0]).geturl()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CkanClient:
    """Reads Analyze Boston's CKAN catalog and downloads resource files."""

    def __init__(self, base_url: str = CKAN_BASE_URL, timeout: float = TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(StripDefaultPortRedirect)

    def _open(self, url: str):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self._opener.open(request, timeout=self.timeout)

    def resource(self, resource_id: str) -> dict:
        query = urllib.parse.urlencode({"id": resource_id})
        with self._open(f"{self.base_url}/api/3/action/resource_show?{query}") as response:
            payload = json.load(response)
        if not payload.get("success"):
            raise RuntimeError(f"catalog lookup failed for {resource_id}: {payload.get('error')}")
        return payload["result"]

    def download(self, url: str, dest: Path) -> None:
        """Stream url to dest. File URLs redirect to signed S3 links, which urllib follows."""
        with self._open(url) as response, open(dest, "wb") as f:
            expected = response.headers.get("Content-Length")
            written = 0
            while chunk := response.read(CHUNK_BYTES):
                f.write(chunk)
                written += len(chunk)
        if expected is not None and written != int(expected):
            raise OSError(f"download truncated: got {written} of {expected} bytes")


def catalog_fingerprint(resource: dict) -> dict[str, str]:
    return {k: "" if resource.get(k) is None else str(resource[k]) for k in CATALOG_FIELDS}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    os.replace(tmp, path)


def load_manifest(state_dir: Path, raw_dir: Path | None = None) -> dict:
    """The schedule state from the state folder.

    Before #6's review the state lived in the raw folder. When the state folder
    has none yet, that copy is read instead, never moved or changed, so an
    existing download history is kept and nothing is downloaded again.
    """
    path = state_dir / MANIFEST
    if not path.exists() and raw_dir is not None and (raw_dir / MANIFEST).exists():
        path = raw_dir / MANIFEST
        log.warning("no schedule state in %s yet; reading %s (move it there, see the runbook)", state_dir, path)
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path} is not valid JSON ({exc}). Fix it, or delete it to re-download every file."
            ) from exc
    for section in ("sources", "prepared"):
        manifest.setdefault(section, {})
    return manifest


def due_reason(source: Source, state: dict, has_file: bool, today: date) -> str | None:
    """Why the source should be checked today, or None when it is not due yet.

    Ages are in calendar days, so a daily run at a slightly earlier time than
    yesterday's is still due.
    """
    if not has_file:
        return "no local copy"
    last = state.get("last_checked")
    if not last:
        return "not checked before"
    age = (today - date.fromisoformat(last)).days
    if age >= source.refresh_days:
        return f"last checked {age} day(s) ago, refresh every {source.refresh_days}"
    return None


def validate_download(path: Path, required: set[str]) -> tuple[int, int]:
    """Return (records, malformed records), or raise if the file must not replace the current one."""
    if path.stat().st_size == 0:
        raise ValueError("download is empty")
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        header = next(csv.reader(f), None) or []
    columns = {c.strip().lstrip("﻿") for c in header}
    missing = required - columns
    if missing:
        raise ValueError(f"download is missing columns {sorted(missing)}, so it is not a 311 CSV")
    records, bad = scan_records(path)
    if records == 0:
        raise ValueError("download has a header but no records")
    return records, len(bad)


def refresh_source(
    source: Source,
    state: dict,
    raw_dir: Path,
    client,
    today: date,
    now: datetime,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Check one source and replace its file if it changed. Updates state in place."""
    target = raw_dir / source.filename
    result = {
        "source": source.source,
        "file": source.filename,
        "action": "skipped",
        "reason": "",
        "rows": state.get("rows"),
        "previous_rows": state.get("rows"),
        "malformed_rows": state.get("malformed_rows"),
        "bytes": state.get("bytes"),
    }
    reason = "forced" if force else due_reason(source, state, target.exists(), today)
    if reason is None:
        result["reason"] = f"not due: checked {state['last_checked']}, refresh every {source.refresh_days} day(s)"
        return result

    resource = client.resource(source.resource_id)
    fingerprint = catalog_fingerprint(resource)
    detectable = source.change_detection == "catalog" and any(fingerprint.values())
    if detectable and not force and target.exists() and fingerprint == state.get("fingerprint"):
        result["reason"] = f"{reason}; catalog unchanged since last download"
        if not dry_run:
            state["last_checked"] = today.isoformat()
        return result
    if dry_run:
        result.update(action="would_download", reason=reason)
        return result

    url = resource.get("url")
    if not url:
        raise ValueError(f"catalog has no download URL for resource {source.resource_id}")

    fd, tmp_name = tempfile.mkstemp(dir=raw_dir, prefix=f".{source.filename}.", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        client.download(url, tmp)
        records, malformed = validate_download(tmp, source.required_columns)
        size = tmp.stat().st_size
        digest = file_sha256(tmp)
        current = state.get("sha256") or (file_sha256(target) if target.exists() else None)
        if target.exists() and digest == current:
            result["reason"] = f"{reason}; downloaded, content identical to current file"
        else:
            result["action"] = "updated" if target.exists() else "downloaded"
            result["reason"] = reason
            os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    state.update({
        "file": source.filename,
        "resource_id": source.resource_id,
        "last_checked": today.isoformat(),
        "last_downloaded": now.isoformat(timespec="seconds"),
        "fingerprint": fingerprint,
        "sha256": digest,
        "bytes": size,
        "rows": records,
        "malformed_rows": malformed,
    })
    result.update(rows=records, malformed_rows=malformed, bytes=size)
    return result


def prepare_new_system(raw_path: Path, out_path: Path) -> dict:
    """Write the new-system records plus a source_system column to parquet.

    The raw download is only read. Every value stays text, so the timestamps
    keep their original form. A record whose field count differs from the header
    is left out of the parquet but counted, and stays in the raw file.
    """
    with open(raw_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            raise ValueError(f"{raw_path.name} has no header")
        header = [header[0].lstrip("﻿"), *header[1:]]
        if "source_system" in header:
            raise ValueError(f"{raw_path.name} already has a source_system column")
        rows, malformed, records = [], [], 0
        for fields in reader:
            if not fields:
                continue
            if len(fields) == len(header):
                rows.append([v if v != "" else None for v in fields])
            else:
                malformed.append(records)
            records += 1

    df = pd.DataFrame(rows, columns=header, dtype="string")
    df["source_system"] = pd.Series(NEW_SOURCE_SYSTEM, index=df.index, dtype="string")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(f".{out_path.name}.part")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)
    return {
        "records": records,
        "rows": len(df),
        "malformed_rows": len(malformed),
        "malformed_record_positions": malformed[:20],
        "duplicate_case_ids": int(df["case_id"].duplicated().sum()) if "case_id" in df else 0,
    }


def prepare_all(raw_dir: Path, prepared_dir: Path, sources: list[Source], manifest: dict) -> list[dict]:
    """Write the as-published parquet of each new-system source, unless prepared_dir
    already holds the one last written from the current raw file."""
    results = []
    for source in sources:
        name = source.prepared_name
        state = manifest["sources"].get(source.source, {})
        raw = raw_dir / source.filename
        if name is None or not raw.exists() or not state.get("sha256"):
            continue
        out = prepared_dir / name
        previous = manifest["prepared"].get(name, {})
        # Compare the file itself too: a run with another --prepared-dir (a DAG
        # slice) records its own copy here, which says nothing about this one.
        if out.exists() and previous.get("from_sha256") == state["sha256"] and file_sha256(out) == previous.get("sha256"):
            results.append({"file": name, "action": "skipped", "reason": "already written from the current raw file", "rows": previous.get("rows")})
            continue
        try:
            info = prepare_new_system(raw, out)
        except Exception as exc:
            log.error("prepare %s failed: %s", name, exc)
            results.append({"file": name, "action": "failed", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if info["malformed_rows"]:
            log.warning("%s: %d malformed record(s) kept out of %s, counted here", source.filename, info["malformed_rows"], name)
        manifest["prepared"][name] = {**info, "from_sha256": state["sha256"], "sha256": file_sha256(out)}
        results.append({"file": name, "action": "written", "reason": "raw file changed or parquet missing", **info})
        log.info("%s: wrote %d rows (%d malformed counted)", name, info["rows"], info["malformed_rows"])
    return results


def run_ingestion(
    raw_dir: Path | str,
    sources: list[Source],
    client,
    *,
    state_dir: Path | str,
    prepared_dir: Path | str,
    now: datetime | None = None,
    force: bool = False,
    only: list[str] | None = None,
    system: str | None = None,
    dry_run: bool = False,
) -> dict:
    """One ingestion run. Returns the run record; run["failed"] lists anything that failed."""
    raw_dir, state_dir, prepared_dir = Path(raw_dir), Path(state_dir), Path(prepared_dir)
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if system:
        if system not in SYSTEMS:
            raise ValueError(f"unknown system {system!r}; expected one of {sorted(SYSTEMS)}")
        sources = [s for s in sources if s.system == system]
        if not sources:
            raise ValueError(f"the source list has no {system} sources")
    if only:
        unknown = sorted(set(only) - {s.source for s in sources})
        if unknown:
            raise ValueError(f"unknown source(s) {unknown}; known: {[s.source for s in sources]}")
        sources = [s for s in sources if s.source in only]

    if not dry_run:
        raw_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        for stale in raw_dir.glob(".*.part"):
            stale.unlink()
    manifest = load_manifest(state_dir, raw_dir)

    results = []
    for source in sources:
        state = manifest["sources"].setdefault(source.source, {})
        try:
            result = refresh_source(source, state, raw_dir, client, today, now, force, dry_run)
        except Exception as exc:
            log.error("%s failed, current file kept: %s", source.source, exc)
            result = {
                "source": source.source,
                "file": source.filename,
                "action": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "rows": state.get("rows"),
                "previous_rows": state.get("rows"),
                "malformed_rows": state.get("malformed_rows"),
                "bytes": state.get("bytes"),
            }
        results.append(result)
        log.info("%s %s rows=%s (%s)", source.source, result["action"], result["rows"], result["reason"])
        if not dry_run:
            write_json_atomic(state_dir / MANIFEST, manifest)

    run = {"run_at": now.isoformat(timespec="seconds"), "dry_run": dry_run, "files": results}
    failed = [r["source"] for r in results if r["action"] == "failed"]
    if dry_run:
        run["failed"] = failed
        return run

    run["prepared"] = prepare_all(raw_dir, prepared_dir, sources, manifest)
    failed += [p["file"] for p in run["prepared"] if p["action"] == "failed"]
    run["failed"] = failed

    write_json_atomic(state_dir / MANIFEST, manifest)
    with open(state_dir / RUN_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(run, default=str) + "\n")
    return run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/311", help="the downloads, and nothing else")
    parser.add_argument("--state-dir", default="Data-Pipeline/data/state/311", help="schedule state and run log")
    parser.add_argument("--prepared-dir", default="Data-Pipeline/data/interim", help=f"where new_system{PREPARED_SUFFIX} goes")
    parser.add_argument("--sources", default="Data-Pipeline/config/ingest_311_sources.csv")
    parser.add_argument("--base-url", default=CKAN_BASE_URL)
    parser.add_argument("--system", choices=sorted(SYSTEMS), help="limit the run to one system's sources")
    parser.add_argument("--only", nargs="+", metavar="SOURCE", help="limit the run to these sources")
    parser.add_argument("--force", action="store_true", help="download every selected source, ignoring the schedule")
    parser.add_argument("--dry-run", action="store_true", help="check the catalog and print the plan; write nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    run = run_ingestion(
        args.raw_dir,
        load_sources(args.sources),
        CkanClient(args.base_url),
        state_dir=args.state_dir,
        prepared_dir=args.prepared_dir,
        force=args.force,
        only=args.only,
        system=args.system,
        dry_run=args.dry_run,
    )
    table = pd.DataFrame(run["files"])[["source", "action", "rows", "previous_rows", "reason"]]
    print(table.astype({"rows": "Int64", "previous_rows": "Int64"}).to_string(index=False))
    for p in run.get("prepared", []):
        print(f"{p['file']}: {p['action']} ({p['reason']}), rows={p.get('rows')}")
    if run["failed"]:
        print(f"FAILED: {', '.join(run['failed'])}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
