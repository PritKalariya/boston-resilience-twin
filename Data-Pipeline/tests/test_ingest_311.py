import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import ingest_311 as ing
from legacy_311 import read_years, scan_records

REPO_SOURCES = Path(__file__).resolve().parents[1] / "config" / "ingest_311_sources.csv"
DAY0 = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
LEGACY_HEADER = "case_enquiry_id,open_dt,closed_dt,type,neighborhood,latitude,longitude\n"
NEW_CSV = (
    "case_id,open_date,close_date,service_name,neighborhood\n"
    "BCS-1,2026-07-14 10:56:30.994145+00,2026-07-14 23:42:16+00,Litter & Debris,South End\n"
    "BCS-2,2026-07-15 03:01:02+00,,Street Light Outage,Dorchester\n"
    'BCS-3,2026-10-14 23:59:59.5+00,,"Tree Emergency, Large",Roxbury\n'
).encode()

SOURCES = [
    ing.Source("legacy_2015", "r2015", "2015.csv", "legacy", 30, "catalog"),
    ing.Source("legacy_2025", "r2025", "2025.csv", "legacy", 7, "catalog"),
    ing.Source("legacy_2026", "r2026", "2026.csv", "legacy", 1, "catalog"),
    ing.Source("new_system", "rnew", "new_system.csv", "new_system", 1, "none"),
]


def legacy_csv(year, n):
    rows = "".join(
        f"{year}{i:05d},{year}-01-0{1 + i % 9} 08:00:00{'.5' if i % 2 else ''},,Pothole,Roxbury,42.3,-71.0\n"
        for i in range(n)
    )
    return (LEGACY_HEADER + rows).encode()


class FakeCatalog:
    """Stands in for CkanClient: catalog metadata and file bodies by resource id."""

    def __init__(self):
        self.files = {"r2015": legacy_csv(2015, 3), "r2025": legacy_csv(2025, 4), "r2026": legacy_csv(2026, 5), "rnew": NEW_CSV}
        self.meta = {
            rid: {"hash": f"h-{rid}-1", "last_modified": "2026-09-18T01:00:00", "size": str(len(body))}
            for rid, body in self.files.items()
        }
        self.lookups, self.downloads = [], []
        self.lookup_errors, self.download_errors = {}, {}

    def change(self, rid, body):
        self.files[rid] = body
        self.meta[rid] = {**self.meta[rid], "hash": self.meta[rid]["hash"] + "x", "size": str(len(body))}

    def resource(self, rid):
        self.lookups.append(rid)
        if rid in self.lookup_errors:
            raise self.lookup_errors[rid]
        return {**self.meta[rid], "url": f"https://example.test/{rid}.csv"}

    def download(self, url, dest):
        rid = url.rsplit("/", 1)[1][: -len(".csv")]
        self.downloads.append(rid)
        body = self.files[rid]
        if rid in self.download_errors:
            Path(dest).write_bytes(body[: len(body) // 2])
            raise self.download_errors[rid]
        Path(dest).write_bytes(body)


class FakeStorage:
    """Stands in for google.cloud.storage.Client."""

    def __init__(self, fail_keys=()):
        self.objects, self.uploads, self.fail_keys = {}, [], set(fail_keys)

    def bucket(self, name):
        self.bucket_name = name
        return self

    def blob(self, key):
        storage = self

        class Blob:
            def upload_from_filename(self, filename):
                if key in storage.fail_keys:
                    storage.fail_keys.discard(key)
                    raise ConnectionError("simulated outage")
                storage.objects[key] = Path(filename).read_bytes()
                storage.uploads.append(key)

        return Blob()


@pytest.fixture
def catalog():
    return FakeCatalog()


@pytest.fixture
def raw(tmp_path):
    return tmp_path / "raw" / "311"


def run(raw, catalog, when=DAY0, **kwargs):
    return ing.run_ingestion(raw, SOURCES, catalog, now=when, **kwargs)


def actions(run_record):
    return {r["source"]: r["action"] for r in run_record["files"]}


def run_log(raw):
    return [json.loads(line) for line in (raw / ing.RUN_LOG).read_text().splitlines()]


# Config


def test_repo_config_matches_refresh_schedule():
    sources = {s.source: s for s in ing.load_sources(REPO_SOURCES)}
    assert len(sources) == 13
    for year in range(2015, 2025):
        s = sources[f"legacy_{year}"]
        assert (s.filename, s.system, s.refresh_days, s.change_detection) == (f"{year}.csv", "legacy", 30, "catalog")
    assert sources["legacy_2025"].refresh_days == 7
    assert sources["legacy_2026"].refresh_days == 1
    new = sources["new_system"]
    assert (new.filename, new.system, new.refresh_days, new.change_detection) == ("new_system.csv", "new_system", 1, "none")
    assert new.prepared_name == "prepared/new_system.parquet"


GOOD_ROW = "legacy_2015,r1,2015.csv,legacy,30,catalog\n"


@pytest.mark.parametrize(
    "body, message",
    [
        ("source,resource_id,filename,system,refresh_days\n" + "a,b,c.csv,legacy,1\n", "missing columns"),
        (",".join(ing.SOURCE_COLUMNS) + "\n", "no sources"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,r1,2015.csv,legacy,0,catalog\n", "refresh_days"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,r1,2015.csv,legacy,weekly,catalog\n", "refresh_days"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,r1,2015.csv,old,30,catalog\n", "system"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,r1,2015.csv,legacy,30,etag\n", "change_detection"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,r1,../2015.csv,legacy,30,catalog\n", "bad filename"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,r1,_manifest.csv,legacy,30,catalog\n", "bad filename"),
        (",".join(ing.SOURCE_COLUMNS) + "\nlegacy_2015,,2015.csv,legacy,30,catalog\n", "resource_id"),
        (",".join(ing.SOURCE_COLUMNS) + "\n" + GOOD_ROW + GOOD_ROW.replace("2015.csv", "x.csv"), "repeats source"),
        (",".join(ing.SOURCE_COLUMNS) + "\n" + GOOD_ROW + GOOD_ROW.replace("legacy_2015", "other"), "repeats filename"),
    ],
)
def test_bad_config_is_rejected(tmp_path, body, message):
    path = tmp_path / "sources.csv"
    path.write_text(body)
    with pytest.raises(ValueError, match=message):
        ing.load_sources(path)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ing.load_sources(tmp_path / "nope.csv")


# Schedule and change detection


def test_first_run_downloads_every_file(raw, catalog):
    result = run(raw, catalog)
    assert actions(result) == {s.source: "downloaded" for s in SOURCES}
    assert result["failed"] == []
    for s in SOURCES:
        assert (raw / s.filename).read_bytes() == catalog.files[s.resource_id]


def test_rerun_same_day_skips_everything_without_requests(raw, catalog):
    run(raw, catalog)
    before = {s.filename: (raw / s.filename).read_bytes() for s in SOURCES}
    catalog.lookups.clear()
    catalog.downloads.clear()

    second = run(raw, catalog, when=DAY0 + timedelta(hours=2))
    assert set(actions(second).values()) == {"skipped"}
    assert all(r["reason"].startswith("not due") for r in second["files"])
    assert catalog.lookups == [] and catalog.downloads == []
    assert {s.filename: (raw / s.filename).read_bytes() for s in SOURCES} == before
    assert [p["action"] for p in second["prepared"]] == ["skipped"]


@pytest.mark.parametrize(
    "days_later, checked",
    [
        (1, {"r2026", "rnew"}),
        (6, {"r2026", "rnew"}),
        (7, {"r2025", "r2026", "rnew"}),
        (29, {"r2025", "r2026", "rnew"}),
        (30, {"r2015", "r2025", "r2026", "rnew"}),
    ],
)
def test_each_file_is_checked_on_its_own_interval(raw, catalog, days_later, checked):
    run(raw, catalog)
    catalog.lookups.clear()
    run(raw, catalog, when=DAY0 + timedelta(days=days_later))
    assert set(catalog.lookups) == checked


def test_a_month_of_daily_runs_never_redownloads_unchanged_history(raw, catalog):
    run(raw, catalog)
    catalog.lookups.clear()
    catalog.downloads.clear()
    for day in range(1, 31):
        result = run(raw, catalog, when=DAY0 + timedelta(days=day))
        assert result["failed"] == []
    lookups = {rid: catalog.lookups.count(rid) for rid in catalog.files}
    assert lookups == {"r2015": 1, "r2025": 4, "r2026": 30, "rnew": 30}
    # Catalog metadata never changed, so only the source without change detection is fetched.
    assert set(catalog.downloads) == {"rnew"} and len(catalog.downloads) == 30


def test_due_is_by_calendar_day_not_24_hours(raw, catalog):
    run(raw, catalog, when=datetime(2026, 10, 15, 23, 59, tzinfo=timezone.utc))
    catalog.lookups.clear()
    run(raw, catalog, when=datetime(2026, 10, 16, 0, 1, tzinfo=timezone.utc))
    assert "r2026" in catalog.lookups


def test_unchanged_catalog_skips_download(raw, catalog):
    run(raw, catalog)
    catalog.downloads.clear()
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    row = next(r for r in result["files"] if r["source"] == "legacy_2026")
    assert row["action"] == "skipped" and "catalog unchanged" in row["reason"]
    assert "r2026" not in catalog.downloads
    assert row["rows"] == 5


def test_source_without_change_detection_downloads_when_due(raw, catalog):
    run(raw, catalog)
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    row = next(r for r in result["files"] if r["source"] == "new_system")
    assert catalog.downloads.count("rnew") == 2
    assert row["action"] == "skipped" and "content identical" in row["reason"]

    catalog.files["rnew"] = NEW_CSV + b"BCS-4,2026-10-15 01:00:00+00,,Pothole,Roxbury\n"
    result = run(raw, catalog, when=DAY0 + timedelta(days=2))
    assert actions(result)["new_system"] == "updated"
    assert (raw / "new_system.csv").read_bytes() == catalog.files["rnew"]


def test_catalog_without_metadata_falls_back_to_interval_download(raw, catalog):
    catalog.meta["r2026"] = {"hash": "", "last_modified": None, "size": ""}
    run(raw, catalog)
    catalog.downloads.clear()
    run(raw, catalog, when=DAY0 + timedelta(days=1))
    assert "r2026" in catalog.downloads


def test_changed_file_is_replaced_not_appended(raw, catalog):
    run(raw, catalog)
    catalog.change("r2026", legacy_csv(2026, 7))
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    row = next(r for r in result["files"] if r["source"] == "legacy_2026")
    assert (row["action"], row["rows"], row["previous_rows"]) == ("updated", 7, 5)
    assert (raw / "2026.csv").read_bytes() == legacy_csv(2026, 7)

    df, reports = read_years(raw, [2026])
    assert len(df) == 7 and df["case_enquiry_id"].is_unique
    assert reports[0].dropped_rows == 0


def test_catalog_change_with_identical_content_keeps_file(raw, catalog):
    run(raw, catalog)
    catalog.change("r2026", catalog.files["r2026"])
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    row = next(r for r in result["files"] if r["source"] == "legacy_2026")
    assert row["action"] == "skipped" and "content identical" in row["reason"]
    assert "r2026" in catalog.downloads[-2:]


def test_force_ignores_the_schedule(raw, catalog):
    run(raw, catalog)
    catalog.downloads.clear()
    result = run(raw, catalog, when=DAY0 + timedelta(hours=1), force=True)
    assert sorted(catalog.downloads) == sorted(catalog.files)
    assert all(r["reason"].startswith("forced") for r in result["files"])


def test_only_limits_the_run(raw, catalog):
    result = run(raw, catalog, only=["legacy_2026"])
    assert actions(result) == {"legacy_2026": "downloaded"}
    assert not (raw / "2015.csv").exists()


def test_only_rejects_unknown_source(raw, catalog):
    with pytest.raises(ValueError, match="unknown source"):
        run(raw, catalog, only=["legacy_1999"])


def test_dry_run_writes_nothing(raw, catalog):
    result = run(raw, catalog, dry_run=True)
    assert set(actions(result).values()) == {"would_download"}
    assert catalog.downloads == []
    assert not raw.exists()


def test_local_file_deleted_is_downloaded_even_if_not_due(raw, catalog):
    run(raw, catalog)
    (raw / "2015.csv").unlink()
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    assert actions(result)["legacy_2015"] == "downloaded"
    assert (raw / "2015.csv").read_bytes() == catalog.files["r2015"]


def test_existing_files_without_manifest(raw, catalog):
    raw.mkdir(parents=True)
    (raw / "2015.csv").write_bytes(catalog.files["r2015"])
    (raw / "2025.csv").write_bytes(legacy_csv(2025, 1))
    result = run(raw, catalog)
    assert actions(result)["legacy_2015"] == "skipped"
    assert actions(result)["legacy_2025"] == "updated"
    assert (raw / "2025.csv").read_bytes() == catalog.files["r2025"]


# Empty, missing, and malformed input


def test_empty_download_keeps_current_file_and_stays_due(raw, catalog):
    run(raw, catalog)
    good = (raw / "2026.csv").read_bytes()
    catalog.change("r2026", b"")
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    row = next(r for r in result["files"] if r["source"] == "legacy_2026")
    assert row["action"] == "failed" and "empty" in row["reason"]
    assert row["rows"] == 5
    assert result["failed"] == ["legacy_2026"]
    assert (raw / "2026.csv").read_bytes() == good

    catalog.change("r2026", legacy_csv(2026, 6))
    retry = run(raw, catalog, when=DAY0 + timedelta(days=1, hours=1))
    assert actions(retry)["legacy_2026"] == "updated"


def test_empty_download_on_first_run_creates_no_file(raw, catalog):
    catalog.files["r2015"] = b""
    result = run(raw, catalog)
    assert actions(result)["legacy_2015"] == "failed"
    assert not (raw / "2015.csv").exists()
    assert actions(result)["legacy_2025"] == "downloaded"


def test_missing_resource_is_reported_and_others_continue(raw, catalog):
    catalog.lookup_errors["r2025"] = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    result = run(raw, catalog)
    assert result["failed"] == ["legacy_2025"]
    assert "404" in next(r for r in result["files"] if r["source"] == "legacy_2025")["reason"]
    assert actions(result)["legacy_2026"] == "downloaded"


def test_catalog_without_url_fails(raw, catalog):
    original = catalog.resource
    catalog.resource = lambda rid: {**original(rid), "url": ""}
    result = run(raw, catalog, only=["legacy_2015"])
    assert actions(result) == {"legacy_2015": "failed"}
    assert "no download URL" in result["files"][0]["reason"]


@pytest.mark.parametrize(
    "body, message",
    [
        (b"<!doctype html><html><body>Service unavailable</body></html>", "missing columns"),
        (LEGACY_HEADER.encode(), "no records"),
        (b"id,date\n1,2026-01-01\n", "missing columns"),
        (b"\n\n", "missing columns"),
    ],
)
def test_malformed_download_is_rejected(raw, catalog, body, message):
    run(raw, catalog)
    good = (raw / "2026.csv").read_bytes()
    catalog.change("r2026", body)
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    row = next(r for r in result["files"] if r["source"] == "legacy_2026")
    assert row["action"] == "failed" and message in row["reason"]
    assert (raw / "2026.csv").read_bytes() == good


def test_interrupted_download_leaves_no_partial_file(raw, catalog):
    run(raw, catalog)
    good = (raw / "2026.csv").read_bytes()
    catalog.change("r2026", legacy_csv(2026, 9))
    catalog.download_errors["r2026"] = ConnectionResetError("connection dropped")
    result = run(raw, catalog, when=DAY0 + timedelta(days=1))
    assert actions(result)["legacy_2026"] == "failed"
    assert (raw / "2026.csv").read_bytes() == good
    assert list(raw.glob(".*.part")) == []


def test_stale_partial_files_are_cleaned_up(raw, catalog):
    raw.mkdir(parents=True)
    (raw / ".2026.csv.abc.part").write_bytes(b"half a file")
    run(raw, catalog)
    assert list(raw.glob(".*.part")) == []


def test_corrupt_manifest_stops_the_run(raw, catalog):
    raw.mkdir(parents=True)
    (raw / ing.MANIFEST).write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        run(raw, catalog)


def test_legacy_header_with_byte_order_mark_is_accepted(raw, catalog):
    catalog.files["r2015"] = "﻿".encode() + legacy_csv(2015, 2)
    result = run(raw, catalog, only=["legacy_2015"])
    assert actions(result) == {"legacy_2015": "downloaded"}


# New-system records


def test_new_system_timestamps_are_preserved(raw, catalog):
    run(raw, catalog)
    assert (raw / "new_system.csv").read_bytes() == NEW_CSV
    df = pd.read_parquet(raw / "prepared" / "new_system.parquet")
    assert df["open_date"].tolist() == [
        "2026-07-14 10:56:30.994145+00",
        "2026-07-15 03:01:02+00",
        "2026-10-14 23:59:59.5+00",
    ]
    assert df["close_date"].iloc[0] == "2026-07-14 23:42:16+00"
    assert df["close_date"].iloc[1:].isna().all()
    assert df["service_name"].iloc[2] == "Tree Emergency, Large"
    assert (df["source_system"] == "new_system").all()
    assert list(df.columns) == ["case_id", "open_date", "close_date", "service_name", "neighborhood", "source_system"]


def test_new_system_malformed_rows_are_counted_not_hidden(raw, catalog):
    catalog.files["rnew"] = NEW_CSV + b"BCS-9,2026-10-01 00:00:00+00,,Pothole,Roxbury,extra\n"
    result = run(raw, catalog)
    prepared = result["prepared"][0]
    assert (prepared["records"], prepared["rows"], prepared["malformed_rows"]) == (4, 3, 1)
    assert prepared["malformed_record_positions"] == [3]
    row = next(r for r in result["files"] if r["source"] == "new_system")
    assert (row["rows"], row["malformed_rows"]) == (4, 1)
    assert (raw / "new_system.csv").read_bytes() == catalog.files["rnew"]
    assert len(pd.read_parquet(raw / "prepared" / "new_system.parquet")) == 3


def test_prepared_file_is_rewritten_only_when_raw_changes(raw, catalog):
    run(raw, catalog)
    second = run(raw, catalog, when=DAY0 + timedelta(days=1))
    assert second["prepared"][0]["action"] == "skipped"

    catalog.files["rnew"] = NEW_CSV + b"BCS-4,2026-10-15 01:00:00+00,,Pothole,Roxbury\n"
    third = run(raw, catalog, when=DAY0 + timedelta(days=2))
    assert third["prepared"][0]["action"] == "written"
    assert len(pd.read_parquet(raw / "prepared" / "new_system.parquet")) == 4


def test_prepared_file_is_rebuilt_if_deleted(raw, catalog):
    run(raw, catalog)
    (raw / "prepared" / "new_system.parquet").unlink()
    result = run(raw, catalog, when=DAY0 + timedelta(hours=1))
    assert result["prepared"][0]["action"] == "written"


def test_existing_source_system_column_is_refused(raw, catalog):
    catalog.files["rnew"] = b"case_id,open_date,source_system\nBCS-1,2026-07-14 10:56:30+00,x\n"
    result = run(raw, catalog)
    assert result["prepared"][0]["action"] == "failed"
    assert "prepared/new_system.parquet" in result["failed"]


def test_prepare_counts_duplicate_case_ids_without_dropping(tmp_path):
    raw_path = tmp_path / "new_system.csv"
    raw_path.write_bytes(NEW_CSV + NEW_CSV.splitlines(keepends=True)[1])
    info = ing.prepare_new_system(raw_path, tmp_path / "out.parquet")
    assert (info["rows"], info["duplicate_case_ids"]) == (4, 1)


def test_prepare_header_only_and_no_header(tmp_path):
    header_only = tmp_path / "a.csv"
    header_only.write_text("case_id,open_date\n")
    info = ing.prepare_new_system(header_only, tmp_path / "a.parquet")
    assert (info["records"], info["rows"]) == (0, 0)
    empty = tmp_path / "b.csv"
    empty.write_text("")
    with pytest.raises(ValueError, match="no header"):
        ing.prepare_new_system(empty, tmp_path / "b.parquet")


# Row counts and run log


def test_run_log_has_row_counts_for_every_file_every_run(raw, catalog):
    run(raw, catalog)
    catalog.change("r2026", legacy_csv(2026, 8))
    run(raw, catalog, when=DAY0 + timedelta(days=1))
    entries = run_log(raw)
    assert len(entries) == 2
    for entry in entries:
        assert [f["source"] for f in entry["files"]] == [s.source for s in SOURCES]
    for f in entries[-1]["files"]:
        assert f["rows"] == scan_records(raw / f["file"])[0]
    second = {f["source"]: f for f in entries[1]["files"]}
    assert (second["legacy_2026"]["action"], second["legacy_2026"]["rows"], second["legacy_2026"]["previous_rows"]) == ("updated", 8, 5)
    assert (second["legacy_2015"]["action"], second["legacy_2015"]["rows"]) == ("skipped", 3)


def test_manifest_matches_files_on_disk(raw, catalog):
    run(raw, catalog)
    manifest = json.loads((raw / ing.MANIFEST).read_text())
    for s in SOURCES:
        state = manifest["sources"][s.source]
        assert state["sha256"] == ing.file_sha256(raw / s.filename)
        assert state["rows"] == scan_records(raw / s.filename)[0]
        assert state["last_checked"] == "2026-10-15"
    assert manifest["prepared"]["prepared/new_system.parquet"]["rows"] == 3


def test_failed_run_is_logged_too(raw, catalog):
    catalog.files["r2015"] = b""
    run(raw, catalog)
    entry = run_log(raw)[0]
    assert entry["failed"] == ["legacy_2015"]


# Google Cloud Storage


def test_upload_goes_to_raw_311_and_skips_unchanged_files(raw, catalog):
    storage = FakeStorage()
    uploader = ing.GcsUploader("gs://brt-test-bucket/", client=storage)
    first = run(raw, catalog, uploader=uploader)
    assert storage.bucket_name == "brt-test-bucket"
    assert set(storage.objects) == {
        "raw/311/2015.csv",
        "raw/311/2025.csv",
        "raw/311/2026.csv",
        "raw/311/new_system.csv",
        "raw/311/prepared/new_system.parquet",
        "raw/311/_manifest.json",
        "raw/311/_runs.jsonl",
    }
    assert storage.objects["raw/311/2015.csv"] == catalog.files["r2015"]
    assert first["gcs_prefix"] == "gs://brt-test-bucket/raw/311/"

    storage.uploads.clear()
    run(raw, catalog, uploader=uploader, when=DAY0 + timedelta(days=1))
    assert storage.uploads == ["raw/311/_manifest.json", "raw/311/_runs.jsonl"]

    storage.uploads.clear()
    catalog.change("r2026", legacy_csv(2026, 6))
    run(raw, catalog, uploader=uploader, when=DAY0 + timedelta(days=2))
    assert "raw/311/2026.csv" in storage.uploads
    assert storage.objects["raw/311/2026.csv"] == legacy_csv(2026, 6)


def test_failed_upload_is_retried_on_the_next_run(raw, catalog):
    storage = FakeStorage(fail_keys={"raw/311/2015.csv"})
    uploader = ing.GcsUploader("brt-test-bucket", client=storage)
    first = run(raw, catalog, uploader=uploader)
    assert first["failed"] == ["2015.csv"]
    assert "raw/311/2015.csv" not in storage.objects

    second = run(raw, catalog, uploader=uploader, when=DAY0 + timedelta(hours=1))
    assert second["failed"] == []
    assert storage.objects["raw/311/2015.csv"] == catalog.files["r2015"]


def test_custom_prefix(raw, catalog):
    storage = FakeStorage()
    run(raw, catalog, uploader=ing.GcsUploader("b", prefix="/test/311/", client=storage), only=["legacy_2015"])
    assert "test/311/2015.csv" in storage.objects


@pytest.mark.parametrize("bucket", ["brt-bucket", "gs://brt-bucket", "gs://brt-bucket/", " brt-bucket "])
def test_bucket_name_forms(bucket):
    assert ing.GcsUploader(bucket, client=FakeStorage()).bucket_name == "brt-bucket"


@pytest.mark.parametrize("bucket", ["", "gs://", "gs://brt-bucket/raw/311"])
def test_bad_bucket_name_is_rejected(bucket):
    with pytest.raises(ValueError, match="bucket"):
        ing.GcsUploader(bucket, client=FakeStorage())


def test_uploader_without_google_library_explains(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "google" or name.startswith("google."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="google-cloud-storage"):
        ing.GcsUploader("brt-bucket")


# End to end: real HTTP through a local stand-in for Analyze Boston


@pytest.fixture
def catalog_server():
    files = {"r2015": legacy_csv(2015, 3), "r2025": legacy_csv(2025, 4), "r2026": legacy_csv(2026, 5), "rnew": NEW_CSV}
    truncate, hosts = set(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, content_type, length=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if length is None else length))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            rid = url.path.rsplit("/", 1)[-1]
            if url.path == "/api/3/action/resource_show":
                rid = urllib.parse.parse_qs(url.query).get("id", [""])[0]
                if rid == "unsuccessful":
                    return self.send(200, json.dumps({"success": False, "error": "nope"}).encode(), "application/json")
                if rid not in files:
                    return self.send(404, json.dumps({"success": False}).encode(), "application/json")
                result = {"id": rid, "url": f"{base}/download/{rid}", "hash": "h1", "last_modified": "2026-09-18", "size": "1"}
                return self.send(200, json.dumps({"success": True, "result": result}).encode(), "application/json")
            if url.path.startswith("/download/"):
                self.send_response(302)
                self.send_header("Location", f"{base}/s3/{rid}?X-Amz-Signature=abc")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            if url.path.startswith("/s3/") and rid in files:
                hosts.append(self.headers["Host"])
                length = len(files[rid]) + 100 if rid in truncate else None
                return self.send(200, files[rid], "text/csv", length)
            return self.send(404, b"not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_address[1]}", files=files, truncate=truncate, hosts=hosts)
    server.shutdown()
    server.server_close()


def write_sources(tmp_path, sources=SOURCES):
    path = tmp_path / "sources.csv"
    lines = [",".join(ing.SOURCE_COLUMNS)]
    lines += [f"{s.source},{s.resource_id},{s.filename},{s.system},{s.refresh_days},{s.change_detection}" for s in sources]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_end_to_end_cli(tmp_path, raw, catalog_server, monkeypatch, capsys):
    monkeypatch.delenv("BRT_GCS_BUCKET", raising=False)
    args = ["--raw-dir", str(raw), "--sources", str(write_sources(tmp_path)), "--base-url", catalog_server.base]

    assert ing.main(args) == 0
    out = capsys.readouterr().out
    assert out.count("downloaded") == 4
    for s in SOURCES:
        assert (raw / s.filename).read_bytes() == catalog_server.files[s.resource_id]

    # Srikar's parser reads the downloaded legacy files with nothing dropped.
    df, reports = read_years(raw, [2015, 2025, 2026])
    assert len(df) == 3 + 4 + 5
    assert all(r.dropped_rows == 0 for r in reports)
    assert len(pd.read_parquet(raw / "prepared" / "new_system.parquet")) == 3

    assert ing.main(args) == 0
    assert "downloaded" not in capsys.readouterr().out
    assert len(run_log(raw)) == 2
    assert len(read_years(raw, [2015, 2025, 2026])[0]) == 12


def test_end_to_end_dry_run(tmp_path, raw, catalog_server, capsys):
    args = ["--raw-dir", str(raw), "--sources", str(write_sources(tmp_path)), "--base-url", catalog_server.base, "--dry-run"]
    assert ing.main(args) == 0
    assert capsys.readouterr().out.count("would_download") == 4
    assert not raw.exists()


def test_end_to_end_truncated_download_fails(tmp_path, raw, catalog_server, capsys):
    catalog_server.truncate.add("r2026")
    args = ["--raw-dir", str(raw), "--sources", str(write_sources(tmp_path)), "--base-url", catalog_server.base]
    assert ing.main(args) == 1
    assert "FAILED: legacy_2026" in capsys.readouterr().err
    assert not (raw / "2026.csv").exists()
    assert (raw / "2015.csv").exists()


def test_end_to_end_missing_and_unsuccessful_resources(raw, catalog_server):
    sources = [
        ing.Source("gone", "not-there", "gone.csv", "legacy", 1, "catalog"),
        ing.Source("bad", "unsuccessful", "bad.csv", "legacy", 1, "catalog"),
    ]
    result = ing.run_ingestion(raw, sources, ing.CkanClient(catalog_server.base), now=DAY0)
    reasons = {r["source"]: r["reason"] for r in result["files"]}
    assert result["failed"] == ["gone", "bad"]
    assert "404" in reasons["gone"]
    assert "catalog lookup failed" in reasons["bad"]


@pytest.mark.parametrize(
    "location, expected",
    [
        # Analyze Boston's presigned S3 links: S3 rejects a Host header of "s3.amazonaws.com:443".
        (
            "https://s3.amazonaws.com:443/bucket/f.csv?X-Amz-Credential=a%2Fb&X-Amz-Signature=abc",
            "https://s3.amazonaws.com/bucket/f.csv?X-Amz-Credential=a%2Fb&X-Amz-Signature=abc",
        ),
        ("http://example.test:80/f.csv", "http://example.test/f.csv"),
        ("https://example.test:8443/f.csv", "https://example.test:8443/f.csv"),
        ("https://example.test/f.csv", "https://example.test/f.csv"),
    ],
)
def test_redirect_drops_only_default_ports(location, expected):
    original = urllib.request.Request("https://data.boston.gov/dataset/x/download/f.csv", headers={"User-Agent": "t"})
    new = ing.StripDefaultPortRedirect().redirect_request(original, None, 302, "Found", {}, location)
    assert new.full_url == expected
    assert new.host == urllib.parse.urlsplit(expected).netloc


def test_end_to_end_redirect_sends_host_from_location(raw, catalog_server):
    result = ing.run_ingestion(raw, SOURCES[:1], ing.CkanClient(catalog_server.base), now=DAY0)
    assert actions(result) == {"legacy_2015": "downloaded"}
    assert catalog_server.hosts[-1] == urllib.parse.urlsplit(catalog_server.base).netloc


def test_cli_with_bucket_uses_uploader(tmp_path, raw, catalog_server, monkeypatch):
    storage = FakeStorage()
    original = ing.GcsUploader.__init__
    monkeypatch.setattr(ing.GcsUploader, "__init__", lambda self, bucket, prefix: original(self, bucket, prefix, client=storage))
    monkeypatch.setenv("BRT_GCS_BUCKET", "brt-test-bucket")
    args = ["--raw-dir", str(raw), "--sources", str(write_sources(tmp_path)), "--base-url", catalog_server.base]
    assert ing.main(args) == 0
    assert "raw/311/2026.csv" in storage.objects
