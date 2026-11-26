import pytest
from bucket_fakes import FakeClient, FakeGcsBucket, fake_bucket
from google.api_core import exceptions

import data_bucket
from data_bucket import REPO_ROOT, BucketError, DataBucket, NoBucketAccess, md5_base64, remote

EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


def test_the_bucket_and_prefix_come_from_the_dvc_remote():
    assert remote(REPO_ROOT) == ("boston-resilience-twin-data", "dvc")
    assert DataBucket.from_repo(REPO_ROOT, client=object()).name == "boston-resilience-twin-data"


@pytest.mark.parametrize("config", [
    None,  # no .dvc/config
    "[core]\n    remote = other\n['remote \"other\"']\n    url = s3://elsewhere/dvc\n",
    "[core]\n    remote = gcs\n['remote \"gcs\"']\n    url = gs://only-a-bucket\n",
])
def test_a_remote_that_is_not_a_gs_bucket_and_prefix_is_an_error(tmp_path, config):
    if config is not None:
        (tmp_path / ".dvc").mkdir()
        (tmp_path / ".dvc" / "config").write_text(config)
    with pytest.raises(BucketError):
        remote(tmp_path)


def test_md5_base64_is_how_cloud_storage_reports_an_md5():
    assert md5_base64(EMPTY_MD5) == "1B2M2Y8AsgTpgAmY7PhCfg=="


def test_write_read_and_exists():
    bucket = fake_bucket()
    assert bucket.read("manifests/latest.json") is None
    assert not bucket.exists("manifests/latest.json")
    bucket.write("manifests/latest.json", b'{"a":1}', "application/json")
    assert bucket.exists("manifests/latest.json")
    assert bucket.read("manifests/latest.json") == b'{"a":1}'
    assert bucket.fake.objects["manifests/latest.json"][1] == "application/json"
    assert bucket.uri("manifests/latest.json") == "gs://boston-resilience-twin-data/manifests/latest.json"


def test_the_client_is_asked_for_this_bucket():
    gcs = FakeGcsBucket("some-bucket")
    client = FakeClient(gcs)
    DataBucket("some-bucket", client=client).exists("x")
    assert client.asked == ["some-bucket"]


def test_upload_new_writes_once_and_keeps_the_same_content(tmp_path):
    bucket = fake_bucket()
    local = tmp_path / "labels.parquet"
    local.write_bytes(b"")
    assert bucket.upload_new(f"dvc/files/md5/{EMPTY_MD5[:2]}/{EMPTY_MD5[2:]}", local, EMPTY_MD5) is True
    assert bucket.upload_new(f"dvc/files/md5/{EMPTY_MD5[:2]}/{EMPTY_MD5[2:]}", local, EMPTY_MD5) is False
    assert bucket.fake.writes == [f"dvc/files/md5/{EMPTY_MD5[:2]}/{EMPTY_MD5[2:]}"]


def test_other_content_at_an_md5_path_is_an_error(tmp_path):
    bucket = fake_bucket()
    path = f"dvc/files/md5/{EMPTY_MD5[:2]}/{EMPTY_MD5[2:]}"
    bucket.fake.objects[path] = (b"not empty", None)
    local = tmp_path / "f"
    local.write_bytes(b"")
    with pytest.raises(BucketError, match="must hold that content"):
        bucket.upload_new(path, local, EMPTY_MD5)


def test_the_probe_passes_with_every_permission():
    fake_bucket().probe()


def test_the_probe_names_a_missing_permission():
    bucket = fake_bucket(granted=("storage.objects.get", "storage.objects.create"))
    with pytest.raises(NoBucketAccess, match="lack storage.objects.delete on gs://boston-resilience-twin-data"):
        bucket.probe()


@pytest.mark.parametrize("refusal, code", [
    (exceptions.Forbidden("airflow-vm-sa does not have storage.objects.create access"), "403"),
    (exceptions.Unauthorized("Anonymous caller does not have storage.objects.get access"), "401"),
])
@pytest.mark.parametrize("call", [
    lambda b, tmp: b.read("manifests/latest.json"),
    lambda b, tmp: b.exists("manifests/latest.json"),
    lambda b, tmp: b.write("manifests/latest.json", b"{}", "application/json"),
    lambda b, tmp: b.upload_new("dvc/files/md5/d4/1d8cd98f00b204e9800998ecf8427e", tmp, EMPTY_MD5),
    lambda b, tmp: b.probe(),
])
def test_a_refused_call_fails_loudly(tmp_path, refusal, code, call):
    local = tmp_path / "f"
    local.write_bytes(b"")
    with pytest.raises(NoBucketAccess, match=code):
        call(fake_bucket(refuse=refusal), local)


def test_no_credentials_fails_loudly(monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import storage

    def no_credentials(*args, **kwargs):
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(storage, "Client", no_credentials)
    with pytest.raises(NoBucketAccess, match="no Google credentials"):
        DataBucket("boston-resilience-twin-data").read("manifests/latest.json")


def test_importing_the_module_does_not_load_google_cloud_storage():
    import subprocess
    import sys

    code = "import sys, data_bucket; print('google.cloud.storage' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=data_bucket.REPO_ROOT / "Data-Pipeline" / "scripts",
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
