"""The project 1 data bucket as the evening run uses it (#162): read, write and probe objects.

The bucket is the one the repo's DVC remote names (`.dvc/config`, gs://boston-resilience-twin-data/dvc):
the data contracts put forecasts, manifests and monitoring outputs beside the DVC remote in it
(docs/contracts/README.md). On airflow-vm the calls run as airflow-vm-sa, which holds
roles/storage.objectAdmin on the bucket (Deployment/terraform/iam.tf); its credentials come from the
metadata server, like DVC's.

Unlike dvc_push.py, which skips without access so a laptop or CI run still passes, every call here
fails loudly: no credentials, a 401 or a 403 raise NoBucketAccess. The evening run cannot do its job
without the bucket, so a lost grant must stop it and reach Slack, not be logged as a warning.

Objects are addressed by their path inside the bucket (`manifests/latest.json`, `dvc/files/md5/..`).
upload_new writes a content-addressed object only when it is absent: an object at an md5 path never
changes, so an existing one with the same md5 is left alone, and one with another md5 is an error.

google-cloud-storage is imported only when a client is made, so importing this module stays light.
Tests pass a fake client with the same methods.
"""

from __future__ import annotations

import base64
import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# What the evening run needs: read objects, create them, and replace them (an overwrite in Cloud
# Storage needs delete as well as create). roles/storage.objectAdmin holds all three.
PERMISSIONS = ("storage.objects.get", "storage.objects.create", "storage.objects.delete")


class NoBucketAccess(RuntimeError):
    """No credentials, or the bucket refused them."""


class BucketError(RuntimeError):
    """The bucket answered, but not as the evening run needs."""


def remote(repo: Path = REPO_ROOT) -> tuple[str, str]:
    """The DVC remote of `.dvc/config` as (bucket, prefix): gs://boston-resilience-twin-data/dvc gives
    ("boston-resilience-twin-data", "dvc")."""
    config = configparser.ConfigParser()
    config.read(repo / ".dvc" / "config")
    name = config.get("core", "remote", fallback=None)
    url = config.get(f"'remote \"{name}\"'", "url", fallback="") if name else ""
    if not url.startswith("gs://"):
        raise BucketError(f"{repo / '.dvc' / 'config'} names no gs:// remote (got {url or 'none'})")
    bucket, _, prefix = url[len("gs://"):].partition("/")
    if not bucket or not prefix.strip("/"):
        raise BucketError(f"the DVC remote {url} has no bucket or no prefix")
    return bucket, prefix.strip("/")


def md5_base64(md5_hex: str) -> str:
    """An md5 as Cloud Storage reports it (base64 of the digest), from DVC's hex form."""
    return base64.b64encode(bytes.fromhex(md5_hex)).decode("ascii")


def default_client():
    """A google-cloud-storage client from Application Default Credentials (on the VM, airflow-vm-sa)."""
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import storage

    try:
        return storage.Client()
    except DefaultCredentialsError as e:
        raise NoBucketAccess(f"no Google credentials for the data bucket: {e}") from None


class DataBucket:
    """One bucket, with every refusal turned into NoBucketAccess."""

    def __init__(self, name: str, client=None):
        self.name = name
        self._client = client

    @classmethod
    def from_repo(cls, repo: Path = REPO_ROOT, client=None) -> DataBucket:
        return cls(remote(repo)[0], client)

    def uri(self, path: str) -> str:
        return f"gs://{self.name}/{path}"

    def _bucket(self):
        if self._client is None:
            self._client = default_client()
        return self._client.bucket(self.name)

    def _call(self, what: str, path: str, action):
        from google.api_core import exceptions

        try:
            return action()
        except (exceptions.Unauthorized, exceptions.Forbidden) as e:
            raise NoBucketAccess(f"{what} {self.uri(path)} was refused: {e.code} {e.message}") from None

    def exists(self, path: str) -> bool:
        return self._call("checking", path, lambda: self._bucket().blob(path).exists())

    def read(self, path: str) -> bytes | None:
        """The object's bytes, or None when it does not exist."""
        from google.api_core import exceptions

        def download():
            try:
                return self._bucket().blob(path).download_as_bytes()
            except exceptions.NotFound:
                return None

        return self._call("reading", path, download)

    def write(self, path: str, data: bytes, content_type: str) -> None:
        """Create or replace the object."""
        self._call("writing", path, lambda: self._bucket().blob(path).upload_from_string(data, content_type=content_type))

    def upload_new(self, path: str, local: Path, md5_hex: str) -> bool:
        """Upload a content-addressed file unless the object exists. True when it was uploaded, False
        when the same content was already there; BucketError when other content is at that path."""
        from google.api_core import exceptions

        def upload():
            try:
                self._bucket().blob(path).upload_from_filename(str(local), if_generation_match=0)
                return True
            except exceptions.PreconditionFailed:
                existing = self._bucket().get_blob(path)
                if existing is None or existing.md5_hash != md5_base64(md5_hex):
                    found = None if existing is None else existing.md5_hash
                    raise BucketError(f"{self.uri(path)} exists with md5 {found}, not {md5_base64(md5_hex)} "
                                      f"(base64 of {md5_hex}); an md5 path must hold that content") from None
                return False

        return self._call("uploading", path, upload)

    def probe(self) -> None:
        """Fail now, naming what is missing, unless the credentials hold every permission the run needs."""
        granted = set(self._call("checking access to", "", lambda: self._bucket().test_iam_permissions(list(PERMISSIONS))))
        missing = [p for p in PERMISSIONS if p not in granted]
        if missing:
            raise NoBucketAccess(f"these credentials lack {', '.join(missing)} on gs://{self.name}")
