import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from bucket_fakes import fake_bucket
from google.api_core import exceptions
from jsonschema import Draft202012Validator, FormatChecker

import live_manifest as lm
from data_bucket import REPO_ROOT, NoBucketAccess, remote

CONTRACTS = REPO_ROOT / "docs" / "contracts"
FIXTURE = CONTRACTS / "fixtures" / "manifests" / "2026-11-25.json"
SCHEMA = CONTRACTS / "schemas" / "data-manifest.schema.json"
AS_OF = date(2026, 11, 25)
COMMIT = "13c4170bf8a56bcb97f7c1c17e6ae3df5906027d"
RUN_ID = "scheduled__2026-11-25T23:05:00+00:00"
NOW = datetime(2026, 11, 25, 23, 31, 42, 123456, tzinfo=timezone.utc)
DVC_CONFIG = "[core]\n    remote = gcs\n['remote \"gcs\"']\n    url = gs://boston-resilience-twin-data/dvc\n"


def validator():
    return Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker())


# ---- the contract's fixture ----

def test_the_fixture_is_canonical_and_gives_its_data_version():
    data = FIXTURE.read_bytes()
    assert lm.canonical(json.loads(data)) == data
    assert lm.data_version(data) == (FIXTURE.parent / "data_version.txt").read_text().strip()
    assert (FIXTURE.parent / "latest.json").read_bytes() == data


def test_build_gives_the_fixture_byte_for_byte():
    fixture = json.loads(FIXTURE.read_bytes())
    manifest = lm.build(
        as_of=AS_OF,
        created_at=datetime(2026, 11, 25, 23, 31, 42, tzinfo=timezone.utc),
        commit=fixture["vm_git_commit"],
        dag_run_id=fixture["dag_run_id"],
        files=fixture["files"],
        catalog_sha=fixture["feature_catalog_sha256"],
        last_final=lm.last_final_label_date(AS_OF, newest=date(2026, 11, 24)),  # labels run to yesterday
    )
    assert lm.canonical(manifest) == FIXTURE.read_bytes()
    assert not list(validator().iter_errors(manifest))


def test_the_content_paths_follow_the_dvc_remote():
    bucket, prefix = remote(REPO_ROOT)
    for entry in json.loads(FIXTURE.read_bytes())["files"].values():
        assert f"gs://{bucket}/{lm.object_path(prefix, entry['md5'])}" == entry["content_path"]


# ---- the pieces ----

def test_floats_are_refused():
    with pytest.raises(lm.ManifestError, match="floats at files.labels.size_bytes"):
        lm.canonical({"files": {"labels": {"size_bytes": 1.0}}})


def test_the_timestamp_is_whole_seconds_in_utc():
    assert lm.utc_stamp(NOW) == "2026-11-25T23:31:42Z"
    assert lm.utc_stamp(datetime(2026, 11, 25, 18, 31, 42, tzinfo=timezone(timedelta(hours=-5)))) == "2026-11-25T23:31:42Z"
    with pytest.raises(lm.ManifestError, match="no time zone"):
        lm.utc_stamp(datetime(2026, 11, 25, 23, 31, 42))


def test_file_md5_is_the_md5_of_the_bytes(tmp_path):
    path = tmp_path / "big.parquet"
    data = bytes(range(256)) * 9000  # over 2 MiB, so several 1 MiB blocks
    path.write_bytes(data)
    assert lm.file_md5(path) == hashlib.md5(data).hexdigest()


def test_the_catalog_hash_ignores_a_crlf_checkout(tmp_path):
    lf, crlf = tmp_path / "lf.csv", tmp_path / "crlf.csv"
    lf.write_bytes(b"column,use_in_model\nunit,no\n")
    crlf.write_bytes(b"column,use_in_model\r\nunit,no\r\n")
    assert lm.catalog_sha256(lf) == lm.catalog_sha256(crlf) == hashlib.sha256(lf.read_bytes()).hexdigest()


def write_git(root, head, refs=None, packed=None):
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head)
    for name, sha in (refs or {}).items():
        (git / name).write_text(sha + "\n")
    if packed is not None:
        (git / "packed-refs").write_text(packed)


def test_git_head_reads_a_loose_ref(tmp_path):
    write_git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": COMMIT})
    assert lm.git_head(tmp_path) == COMMIT


def test_git_head_reads_packed_refs(tmp_path):
    write_git(tmp_path, "ref: refs/heads/main\n",
              packed=f"# pack-refs with: peeled fully-peeled sorted\n{'0' * 40} refs/heads/other\n{COMMIT} refs/heads/main\n")
    assert lm.git_head(tmp_path) == COMMIT


def test_git_head_reads_a_detached_head(tmp_path):
    write_git(tmp_path, COMMIT + "\n")
    assert lm.git_head(tmp_path) == COMMIT


@pytest.mark.parametrize("head", ["ref: refs/heads/gone\n", "not a commit\n", None])
def test_git_head_that_resolves_to_no_commit_is_an_error(tmp_path, head):
    if head is not None:
        write_git(tmp_path, head)
    with pytest.raises(lm.ManifestError):
        lm.git_head(tmp_path)


def write_labels(path, last_day):
    path.parent.mkdir(parents=True, exist_ok=True)
    days = [datetime.combine(last_day - timedelta(days=n), datetime.min.time()) for n in range(3)]
    pq.write_table(pa.table({"unit": ["Allston"] * 3, "date": pa.array(days, pa.timestamp("us"))}), path)


@pytest.mark.parametrize("newest, expected", [
    (date(2026, 11, 24), date(2026, 11, 22)),  # labels to yesterday: as_of - 3
    (date(2026, 11, 20), date(2026, 11, 20)),  # labels end earlier: their last day
])
def test_last_final_label_date(tmp_path, newest, expected):
    labels = tmp_path / "labels.parquet"
    write_labels(labels, newest)
    assert lm.newest_label_day(labels) == newest
    assert lm.last_final_label_date(AS_OF, lm.newest_label_day(labels)) == expected


def test_a_last_final_day_too_close_to_as_of_is_refused():
    with pytest.raises(lm.ManifestError, match="less than 2 days before"):
        lm.build(as_of=AS_OF, created_at=NOW, commit=COMMIT, dag_run_id=RUN_ID, files={}, catalog_sha="0" * 64,
                 last_final=date(2026, 11, 24))


# ---- publishing ----

def manifest_bytes(as_of):
    return lm.canonical({"as_of": as_of.isoformat(), "contract": lm.CONTRACT})


def test_publish_writes_the_dated_file_then_the_same_bytes_to_latest():
    bucket = fake_bucket()
    data = manifest_bytes(AS_OF)
    assert lm.publish(bucket, data, AS_OF) is True
    assert bucket.fake.writes == ["manifests/2026-11-25.json", "manifests/latest.json"]
    assert bucket.read("manifests/latest.json") == bucket.read("manifests/2026-11-25.json") == data


def test_latest_never_moves_back_to_an_older_evening(caplog):
    bucket = fake_bucket()
    newer = manifest_bytes(AS_OF)
    lm.publish(bucket, newer, AS_OF)
    with caplog.at_level(logging.INFO):
        assert lm.publish(bucket, manifest_bytes(date(2026, 11, 24)), date(2026, 11, 24)) is False
    assert bucket.read("manifests/latest.json") == newer
    assert bucket.exists("manifests/2026-11-24.json")
    assert "names 2026-11-25, newer than 2026-11-24" in caplog.text


def test_a_rerun_of_the_same_evening_replaces_both():
    bucket = fake_bucket()
    lm.publish(bucket, manifest_bytes(AS_OF), AS_OF)
    again = lm.canonical({"as_of": AS_OF.isoformat(), "contract": lm.CONTRACT, "dag_run_id": "manual__rerun"})
    assert lm.publish(bucket, again, AS_OF) is True
    assert bucket.read("manifests/latest.json") == bucket.read("manifests/2026-11-25.json") == again


def test_an_unreadable_latest_is_replaced(caplog):
    bucket = fake_bucket()
    bucket.fake.objects["manifests/latest.json"] = (b"not json", "application/json")
    with caplog.at_level(logging.WARNING):
        assert lm.publish(bucket, manifest_bytes(AS_OF), AS_OF) is True
    assert "is not a readable manifest" in caplog.text


# ---- the whole task ----

@pytest.fixture
def repo(tmp_path):
    """A checkout with the evening's three files, the catalog, .dvc/config and .git."""
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(DVC_CONFIG)
    write_git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": COMMIT})
    (tmp_path / lm.CATALOG).parent.mkdir(parents=True)
    (tmp_path / lm.CATALOG).write_bytes(b"column,use_in_model\nunit,no\n")
    write_labels(tmp_path / lm.LABELS, date(2026, 11, 24))
    pq.write_table(pa.table({"unit": ["Allston"], "day_of_week": [2]}), tmp_path / lm.FEATURES)
    rows = lm.forecast_dir(AS_OF, tmp_path) / "features.parquet"
    rows.parent.mkdir(parents=True)
    pq.write_table(pa.table({"unit": ["Allston"], "forecast_date": [date(2026, 11, 26)]}), rows)
    return tmp_path


def test_write_manifest_uploads_the_files_and_publishes_the_manifest(repo):
    bucket = fake_bucket()
    version = lm.write_manifest(AS_OF, RUN_ID, repo=repo, bucket=bucket, now=NOW)

    stored = bucket.read("manifests/2026-11-25.json")
    assert stored == bucket.read("manifests/latest.json") == (lm.forecast_dir(AS_OF, repo) / "manifest.json").read_bytes()
    assert version == "sha256:" + hashlib.sha256(stored).hexdigest()
    manifest = json.loads(stored)
    assert not list(validator().iter_errors(manifest))
    assert manifest["as_of"] == "2026-11-25"
    assert manifest["created_at_utc"] == "2026-11-25T23:31:42Z"
    assert manifest["vm_git_commit"] == COMMIT
    assert manifest["dag_run_id"] == RUN_ID
    assert manifest["last_final_label_date"] == "2026-11-22"
    assert manifest["feature_catalog_sha256"] == hashlib.sha256(b"column,use_in_model\nunit,no\n").hexdigest()
    for name, rel in {"labels": lm.LABELS, "features": lm.FEATURES,
                      "forecast_rows": lm.forecast_dir(AS_OF, repo).relative_to(repo) / "features.parquet"}.items():
        entry, local = manifest["files"][name], (repo / rel).read_bytes()
        assert entry["md5"] == hashlib.md5(local).hexdigest()
        assert entry["size_bytes"] == len(local)
        key = entry["content_path"].removeprefix("gs://boston-resilience-twin-data/")
        assert key == f"dvc/files/md5/{entry['md5'][:2]}/{entry['md5'][2:]}"
        assert bucket.read(key) == local


def test_a_rerun_keeps_the_uploaded_files_and_replaces_the_manifest(repo, caplog):
    bucket = fake_bucket()
    first = lm.write_manifest(AS_OF, RUN_ID, repo=repo, bucket=bucket, now=NOW)
    with caplog.at_level(logging.INFO):
        second = lm.write_manifest(AS_OF, "manual__2026-11-25T23:50:00+00:00", repo=repo, bucket=bucket,
                                   now=NOW + timedelta(minutes=19))
    assert second != first  # the contract: a rerun changes the version
    assert caplog.text.count("already in the bucket") == 3
    assert json.loads(bucket.read("manifests/latest.json"))["dag_run_id"] == "manual__2026-11-25T23:50:00+00:00"


def test_no_manifest_without_the_forecast_rows(repo):
    (lm.forecast_dir(AS_OF, repo) / "features.parquet").unlink()
    bucket = fake_bucket()
    with pytest.raises(lm.ManifestError, match="missing forecast_rows"):
        lm.write_manifest(AS_OF, RUN_ID, repo=repo, bucket=bucket, now=NOW)
    assert bucket.fake.writes == []


def test_identical_files_are_refused(repo):
    (repo / lm.FEATURES).write_bytes((repo / lm.LABELS).read_bytes())
    with pytest.raises(lm.ManifestError, match="two of the files are identical"):
        lm.write_manifest(AS_OF, RUN_ID, repo=repo, bucket=fake_bucket(), now=NOW)


def test_another_bucket_than_the_remote_is_refused(repo):
    with pytest.raises(lm.ManifestError, match="names boston-resilience-twin-data"):
        lm.write_manifest(AS_OF, RUN_ID, repo=repo, bucket=fake_bucket("some-other-bucket"), now=NOW)


def test_a_lost_bucket_fails_the_task(repo):
    refused = fake_bucket(refuse=exceptions.Forbidden("airflow-vm-sa does not have storage.objects.create access"))
    with pytest.raises(NoBucketAccess, match="403"):
        lm.write_manifest(AS_OF, RUN_ID, repo=repo, bucket=refused, now=NOW)
    assert not (lm.forecast_dir(AS_OF, repo) / "manifest.json").exists()
