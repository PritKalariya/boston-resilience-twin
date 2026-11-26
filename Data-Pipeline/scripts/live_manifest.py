"""The data manifest of an evening run (#162): exactly which versioned data a forecast was built from.

The contract is docs/contracts/data-manifest.md (manifest/v1, #159). One manifest per as-of date names:

- files: labels.parquet and features.parquet from the run's full build (Data-Pipeline/data/processed/)
  and the forecast rows #160 writes for the evening (Data-Pipeline/data/forecast/<as_of>/features.parquet),
  each with DVC's md5, its size and its content path, the object in the DVC remote of the same bucket
  (`<remote>/files/md5/<2 hex>/<30 hex>`). The run uploads each file there itself, create-only: a DAG
  run makes no DVC version (push_data uploads only what the committed dvc.lock names), but #167
  downloads these files by md5 and the brief cites the rows by content path, so the objects must exist.
  Nothing in git points at them, so `dvc gc -c` must never run on this remote.
- the as-of date, when the manifest was written, the commit checked out (read from the .git files, so
  no git binary is needed in the container), the DAG run id, the sha256 of the feature catalog as
  committed (LF line endings, so a CRLF working copy hashes the same), and the newest day whose labels
  are final. A day's labels are final 48 hours after the day ends, so on the evening of as_of that day
  is as_of - 3 (the contract's fixture: 2026-11-25 gives 2026-11-22), and never later than the newest
  day labels.parquet holds.

The canonical bytes (sorted keys, compact separators, UTF-8, no trailing newline, no floats) go to
manifests/<as_of>.json, then the same bytes to manifests/latest.json, unless latest.json already names
a newer as_of: a rerun of an older evening never moves it back. The data_version every other contract
carries is "sha256:" plus the sha256 of those bytes. A copy goes to
Data-Pipeline/data/forecast/<as_of>/manifest.json, beside the rows.

No manifest is written without all three files (the contract: a forecast from rows no manifest names
cannot be traced), and a lost bucket fails the task (data_bucket.NoBucketAccess).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from data_bucket import DataBucket, remote

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "manifest/v1"
LATEST = "manifests/latest.json"
LABELS = Path("Data-Pipeline/data/processed/labels.parquet")
FEATURES = Path("Data-Pipeline/data/processed/features.parquet")
FORECAST_ROOT = Path("Data-Pipeline/data/forecast")
CATALOG = Path("Data-Pipeline/config/feature_catalog.csv")
# A day's labels are final 48 hours after the day ends: on the evening of as_of, as_of - 3.
FINAL_AFTER_DAYS = 3
# The contract's own floor: the last final day is at least two days before as_of.
FINAL_AT_LEAST_DAYS = 2
JSON = "application/json"

log = logging.getLogger(__name__)


class ManifestError(RuntimeError):
    """The manifest cannot be written as the contract requires."""


def forecast_dir(as_of: date, repo: Path = REPO_ROOT) -> Path:
    return repo / FORECAST_ROOT / as_of.isoformat()


def manifest_path(as_of: date) -> str:
    return f"manifests/{as_of.isoformat()}.json"


def file_md5(path: Path) -> str:
    """DVC 3's md5 of a file: the md5 of its bytes (as Model-Development/scripts/lock_check.py)."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def object_path(prefix: str, md5: str) -> str:
    """Where DVC 3.67.1 keeps a file in its remote: <prefix>/files/md5/<first 2 hex>/<other 30 hex>."""
    return f"{prefix}/files/md5/{md5[:2]}/{md5[2:]}"


def catalog_sha256(path: Path) -> str:
    """The sha256 of the feature catalog as committed (LF line endings), whatever the checkout did to them."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def packed_ref(git: Path, ref: str) -> str:
    try:
        lines = (git / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha
    return ""


def git_head(repo: Path = REPO_ROOT) -> str:
    """The commit checked out, from .git/HEAD and its ref (a loose file, or packed-refs)."""
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ManifestError(f"cannot read {git / 'HEAD'}: {e}") from None
    if head.startswith("ref: "):
        ref = head[len("ref: "):].strip()
        loose = git / ref
        sha = loose.read_text(encoding="utf-8").strip() if loose.is_file() else packed_ref(git, ref)
    else:
        sha = head
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ManifestError(f"{git} does not resolve HEAD ({head!r}) to a commit")
    return sha


def newest_label_day(labels: Path) -> date:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    newest = pc.max(pq.read_table(labels, columns=["date"])["date"]).as_py()
    if newest is None:
        raise ManifestError(f"{labels} has no rows")
    return newest.date() if isinstance(newest, datetime) else newest


def last_final_label_date(as_of: date, newest: date) -> date:
    return min(as_of - timedelta(days=FINAL_AFTER_DAYS), newest)


def utc_stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ManifestError(f"{moment} has no time zone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(*, as_of: date, created_at: datetime, commit: str, dag_run_id: str, files: dict,
          catalog_sha: str, last_final: date) -> dict:
    if last_final > as_of - timedelta(days=FINAL_AT_LEAST_DAYS):
        raise ManifestError(f"last_final_label_date {last_final} is less than {FINAL_AT_LEAST_DAYS} days before {as_of}")
    return {
        "contract": CONTRACT,
        "as_of": as_of.isoformat(),
        "created_at_utc": utc_stamp(created_at),
        "vm_git_commit": commit,
        "dag_run_id": dag_run_id,
        "files": files,
        "feature_catalog_sha256": catalog_sha,
        "last_final_label_date": last_final.isoformat(),
    }


def floats_in(value, where: str = "") -> list[str]:
    if isinstance(value, float):
        return [where or "the top level"]
    if isinstance(value, dict):
        return [found for key, item in value.items() for found in floats_in(item, f"{where}.{key}" if where else key)]
    if isinstance(value, list):
        return [found for i, item in enumerate(value) for found in floats_in(item, f"{where}[{i}]")]
    return []


def canonical(manifest: dict) -> bytes:
    """The contract's one dump: sorted keys, compact, UTF-8, no trailing newline, and no floats."""
    found = floats_in(manifest)
    if found:
        raise ManifestError(f"a manifest holds integers and strings only; floats at {', '.join(found)}")
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def data_version(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def publish(bucket: DataBucket, data: bytes, as_of: date) -> bool:
    """Write manifests/<as_of>.json, then the same bytes to latest.json unless latest names a newer
    as_of. Returns whether latest.json was written."""
    bucket.write(manifest_path(as_of), data, JSON)
    current = bucket.read(LATEST)
    if current is not None:
        try:
            current_as_of = date.fromisoformat(json.loads(current)["as_of"])
        except (ValueError, KeyError, TypeError):
            log.warning("%s is not a readable manifest; replacing it", bucket.uri(LATEST))
        else:
            if current_as_of > as_of:
                log.info("%s names %s, newer than %s; left as it is", bucket.uri(LATEST), current_as_of, as_of)
                return False
    bucket.write(LATEST, data, JSON)
    return True


def write_manifest(as_of: date, dag_run_id: str, repo: Path = REPO_ROOT, bucket: DataBucket | None = None,
                   now: datetime | None = None) -> str:
    """Upload the evening's three files under their md5, write and publish its manifest, and return
    the data_version."""
    paths = {
        "labels": repo / LABELS,
        "features": repo / FEATURES,
        "forecast_rows": forecast_dir(as_of, repo) / "features.parquet",
    }
    missing = [f"{name} ({path})" for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ManifestError(f"no manifest without all three files; missing {', '.join(missing)}")
    bucket_name, prefix = remote(repo)
    bucket = bucket or DataBucket(bucket_name)
    if bucket.name != bucket_name:
        raise ManifestError(f"the bucket is {bucket.name}, but .dvc/config names {bucket_name}")

    md5s = {name: file_md5(path) for name, path in paths.items()}
    if len(set(md5s.values())) < len(md5s):
        raise ManifestError(f"two of the files are identical ({md5s}); each entry must be a different file")
    files = {}
    for name, path in paths.items():
        key = object_path(prefix, md5s[name])
        uploaded = bucket.upload_new(key, path, md5s[name])
        files[name] = {"md5": md5s[name], "size_bytes": path.stat().st_size, "content_path": bucket.uri(key)}
        log.info("%s: md5 %s, %s", name, md5s[name], "uploaded" if uploaded else "already in the bucket")

    manifest = build(
        as_of=as_of,
        created_at=now or datetime.now(timezone.utc),
        commit=git_head(repo),
        dag_run_id=dag_run_id,
        files=files,
        catalog_sha=catalog_sha256(repo / CATALOG),
        last_final=last_final_label_date(as_of, newest_label_day(paths["labels"])),
    )
    data = canonical(manifest)
    moved = publish(bucket, data, as_of)
    (forecast_dir(as_of, repo) / "manifest.json").write_bytes(data)
    version = data_version(data)
    log.info("wrote %s%s: %s", bucket.uri(manifest_path(as_of)), " and latest.json" if moved else "", version)
    return version
