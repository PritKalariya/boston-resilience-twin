"""An in-memory stand-in for the google-cloud-storage client (#162), with the calls data_bucket makes:
bucket(), blob(), get_blob(), test_iam_permissions(), and a blob's exists(), download_as_bytes(),
upload_from_string(), upload_from_filename(if_generation_match=0) and md5_hash. It raises the same
google.api_core exceptions the real client raises, so data_bucket's error handling is what is tested."""

import base64
import hashlib
from pathlib import Path

from google.api_core import exceptions

import data_bucket

ALL_PERMISSIONS = data_bucket.PERMISSIONS


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    @property
    def md5_hash(self):
        return base64.b64encode(hashlib.md5(self.bucket.objects[self.name][0]).digest()).decode("ascii")

    def exists(self):
        self.bucket.check()
        return self.name in self.bucket.objects

    def download_as_bytes(self):
        self.bucket.check()
        if self.name not in self.bucket.objects:
            raise exceptions.NotFound(f"No such object: {self.bucket.name}/{self.name}")
        return self.bucket.objects[self.name][0]

    def upload_from_string(self, data, content_type=None):
        self.bucket.check()
        self.bucket.objects[self.name] = (bytes(data), content_type)
        self.bucket.writes.append(self.name)

    def upload_from_filename(self, filename, if_generation_match=None):
        self.bucket.check()
        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise exceptions.PreconditionFailed("At least one of the pre-conditions you specified did not hold.")
        self.bucket.objects[self.name] = (Path(filename).read_bytes(), None)
        self.bucket.writes.append(self.name)


class FakeGcsBucket:
    def __init__(self, name, granted=ALL_PERMISSIONS, refuse=None):
        self.name, self.granted, self.refuse = name, set(granted), refuse
        self.objects = {}  # path -> (bytes, content type)
        self.writes = []

    def check(self):
        if self.refuse is not None:
            raise self.refuse

    def blob(self, name):
        return FakeBlob(self, name)

    def get_blob(self, name):
        self.check()
        return FakeBlob(self, name) if name in self.objects else None

    def test_iam_permissions(self, permissions):
        self.check()
        return [p for p in permissions if p in self.granted]


class FakeClient:
    def __init__(self, bucket):
        self.gcs_bucket = bucket
        self.asked = []

    def bucket(self, name):
        self.asked.append(name)
        return self.gcs_bucket


def fake_bucket(name="boston-resilience-twin-data", **kwargs):
    """A DataBucket over a fake client; the fake is at .fake for the test to look into."""
    gcs = FakeGcsBucket(name, **kwargs)
    bucket = data_bucket.DataBucket(name, client=FakeClient(gcs))
    bucket.fake = gcs
    return bucket
