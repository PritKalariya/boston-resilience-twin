"""A small REST client for the Vertex AI Model Registry and the model bucket (#111, ADR-006).

It calls the Vertex AI v1 REST API and the Cloud Storage JSON API through google-auth, with no
Vertex SDK: google-auth and requests already come with dvc-gs, and a plain session is easy to
replace with a fake one in the tests (pass session=).

Credentials are Application Default Credentials: the CI identity from Workload Identity Federation
(#131) in GitHub Actions, or `gcloud auth application-default login` on a laptop. No key file.

Errors:
- NoAccess: no credentials, or project 2 refuses them (401 or 403). The push step turns it into a
  log line and a skip, so a run without project 2 access still succeeds (#111).
- HttpError (a RegistryError with .status): any other refusal; RegistryError: a failed long-running
  operation or a request that did not complete. Either fails the step.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

from registry import RegistryError, Settings

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
API_VERSION = "v1"
STORAGE_UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
STORAGE = "https://storage.googleapis.com/storage/v1"
OPERATION_TIMEOUT_SECONDS = 600
POLL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 60


class NoAccess(RegistryError):
    """No credentials for project 2, or project 2 refuses them."""


class HttpError(RegistryError):
    """A refusal other than 401 or 403, with its HTTP status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def default_session(quota_project: str):
    """An authorized requests session from Application Default Credentials, or NoAccess. Calls are
    billed to quota_project (project 2), so end-user credentials need no quota project of their own."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    from google.auth.transport.requests import AuthorizedSession

    try:
        credentials, _ = google.auth.default(scopes=list(SCOPES), quota_project_id=quota_project)
    except DefaultCredentialsError as exc:
        raise NoAccess(f"no Google credentials: {exc}") from None
    return AuthorizedSession(credentials)


class VertexClient:
    """The registry calls #111 needs: find the model, upload a version, read versions, move aliases,
    and upload model files to a path that is never overwritten."""

    def __init__(self, settings: Settings, session=None, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.session = session if session is not None else default_session(settings.project)
        self.sleep = sleep
        self.base = f"https://{settings.region}-aiplatform.googleapis.com/{API_VERSION}"
        self.parent = f"projects/{settings.project}/locations/{settings.region}"

    # ---- transport

    def _request(self, method: str, url: str, raw: bool = False, **kwargs):
        """The JSON body of a call (raw: its bytes); NoAccess on 401 or 403, HttpError on any other refusal."""
        import requests

        try:
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as exc:
            raise RegistryError(f"{method} {url} did not complete: {exc}") from None
        if response.status_code in (401, 403):
            raise NoAccess(f"{method} {url} refused ({response.status_code}): {_message(response)}")
        if response.status_code >= 400:
            raise HttpError(f"{method} {url} failed ({response.status_code}): {_message(response)}",
                            response.status_code)
        if raw:
            return response.content
        return response.json() if response.content else {}

    def _pages(self, url: str, key: str, params: dict | None = None) -> Iterator[dict]:
        params = dict(params or {})
        while True:
            body = self._request("GET", url, params=params)
            yield from body.get(key, [])
            token = body.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    def wait(self, operation: dict) -> dict:
        """Poll a long-running operation until it is done; its response, or RegistryError."""
        deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
        while not operation.get("done"):
            if time.monotonic() > deadline:
                raise RegistryError(f"operation {operation.get('name')} not done after {OPERATION_TIMEOUT_SECONDS} s")
            self.sleep(POLL_SECONDS)
            operation = self._request("GET", f"{self.base}/{operation['name']}")
        if "error" in operation:
            error = operation["error"]
            raise RegistryError(f"operation {operation.get('name')} failed: {error.get('message', error)}")
        return operation.get("response", {})

    # ---- registry

    def find_model(self) -> str | None:
        """The resource name of the model with settings.model_display_name, None when there is none.
        Fails when two models share the name: the registry must give one answer (ADR-006)."""
        name = self.settings.model_display_name
        found = [m["name"] for m in self._pages(f"{self.base}/{self.parent}/models", "models",
                                                {"filter": f'display_name="{name}"'})]
        if len(found) > 1:
            raise RegistryError(f"{len(found)} registry models are named {name!r}: {found}")
        return found[0] if found else None

    def upload_version(self, model: dict, parent_model: str | None) -> tuple[str, str]:
        """Upload a model (the Vertex Model body: displayName, artifactUri, containerSpec, labels,
        versionAliases, versionDescription ...), as a new version of parent_model when given, else as
        the first version of a new model. Returns (model resource name, version id)."""
        body: dict = {"model": model}
        if parent_model:
            body["parentModel"] = parent_model
        operation = self._request("POST", f"{self.base}/{self.parent}/models:upload", json=body)
        response = self.wait(operation)
        try:
            return response["model"], str(response["modelVersionId"])
        except KeyError:
            raise RegistryError(f"upload response has no model or modelVersionId: {response}") from None

    def get_version(self, model_name: str, ref: str) -> dict:
        """One version by version id or alias (the Vertex name@ref form)."""
        return self._request("GET", f"{self.base}/{model_name}@{ref}")

    def find_alias(self, model_name: str, alias: str) -> dict | None:
        """The version that holds alias, None when no version holds it."""
        for version in self.list_versions(model_name):
            if alias in version.get("versionAliases", []):
                return version
        return None

    def list_versions(self, model_name: str) -> list[dict]:
        return list(self._pages(f"{self.base}/{model_name}:listVersions", "models"))

    def merge_aliases(self, model_name: str, version_id: str, add: tuple[str, ...] = (),
                      remove: tuple[str, ...] = ()) -> dict:
        """Add and remove aliases on one version in one call. Adding an alias another version holds
        moves it here (Vertex: one version per alias)."""
        aliases = [*add, *(f"-{alias}" for alias in remove)]
        if not aliases:
            raise RegistryError("merge_aliases needs an alias to add or remove")
        return self._request("POST", f"{self.base}/{model_name}@{version_id}:mergeVersionAliases",
                             json={"versionAliases": aliases})

    # ---- model files

    def upload_file(self, local: str | Path, object_name: str) -> str:
        """Upload one file to the model bucket and return its gs:// URI. Fails if the object exists:
        a version's files are never overwritten (ADR-006)."""
        bucket = self.settings.model_bucket
        url = f"{STORAGE_UPLOAD}/b/{quote(bucket, safe='')}/o"
        data = Path(local).read_bytes()
        try:
            self._request("POST", url, params={"uploadType": "media", "name": object_name, "ifGenerationMatch": "0"},
                          data=data, headers={"Content-Type": "application/octet-stream"})
        except HttpError as exc:
            if exc.status == 412:
                raise RegistryError(f"gs://{bucket}/{object_name} already exists; model files are never overwritten") from None
            raise
        return f"gs://{bucket}/{object_name}"

    def download_file(self, object_name: str, local: str | Path) -> Path:
        """Download one object of the model bucket to local."""
        bucket = self.settings.model_bucket
        url = f"{STORAGE}/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"
        local = Path(local)
        local.write_bytes(self._request("GET", url, raw=True, params={"alt": "media"}))
        return local


def _message(response) -> str:
    try:
        return response.json().get("error", {}).get("message", "") or response.text[:300]
    except ValueError:
        return response.text[:300]
