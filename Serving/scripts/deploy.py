#!/usr/bin/env python3
"""Build the model service image from the production model and deploy it to both Cloud Run services (#161, ADR-038).

  python Serving/scripts/deploy.py [--version <id>] [--dry-run] [--keep <dir>]

Steps: (1) refuse a tree with uncommitted changes under what the image copies, (2) resolve the registry version
(the `production` alias, or --version), (3) download the files its metadata.json lists and check every sha256,
(4) put model.joblib and metadata.json in one folder, (5) --dry-run stops here, (6) docker build, (7) docker push
and read the digest, (8) point both services at the image digest, (9) print each service's URL and revision.

It runs on a host with only google-auth and requests (Serving/requirements-deploy.txt) and the docker CLI. It
never calls gcloud and imports nothing from Model-Development: registry.py imports training_data, which imports
pandas, and vertex_client.py imports registry.py, so neither can load here. The registry lookup below is the
same REST calls vertex_client.py makes, cut to the alias, the version id and the files folder.

Every setting is read from Model-Development/registry/registry.csv (project, region, registry, model bucket,
image repository) and Serving/deploy.csv (service names, image name, routes). No project id is in this file.

Exit codes: 0 on success, 2 on any failure, which prints one line saying which step failed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve()
# The repository root in a checkout (Serving/scripts/deploy.py), or /app in the test image, which holds
# Serving/deploy.csv and Model-Development/registry/registry.csv beside scripts/.
ROOT = next((p for p in (HERE.parents[2], HERE.parents[1]) if (p / "Serving" / "deploy.csv").exists()), HERE.parents[2])
REGISTRY_CSV = ROOT / "Model-Development" / "registry" / "registry.csv"
DEPLOY_CSV = ROOT / "Serving" / "deploy.csv"
# What the image is built from. An uncommitted change to any of these would put code in the image that no commit has.
CLEAN_PATHS = ("Serving", "docs/contracts/schemas", "Frontend", "Model-Development/requirements.lock")

VERTEX_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
STORAGE = "https://storage.googleapis.com/storage/v1"
RUN_API = "https://run.googleapis.com/v2"
REQUEST_TIMEOUT_SECONDS = 60
OPERATION_TIMEOUT_SECONDS = 600
POLL_SECONDS = 5

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
FILES_FOLDER = re.compile(r"files gs://([^/\s]+)/(\S+?)/ \(metadata\.json\)")  # the push's version description
REGISTRY_SETTINGS = ("project", "region", "model_display_name", "model_bucket", "serving_image", "alias_production")


class DeployError(Exception):
    """A step that cannot go ahead. `step` is the step's name, printed in the one failure line."""

    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step


# ---- settings (reviewed files, not constants)


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(stream)]
    except OSError as exc:
        raise DeployError("settings", f"cannot read {path}: {exc}") from None


@dataclass(frozen=True)
class Settings:
    project: str
    region: str
    model_display_name: str
    model_bucket: str
    alias_production: str
    repository: str  # the Artifact Registry repository path, without the image name
    image_name: str
    services: dict[str, str]  # Cloud Run service name -> its SERVING_ROUTES value


def load_settings(registry_csv: Path | None = None, deploy_csv: Path | None = None) -> Settings:
    registry_csv, deploy_csv = registry_csv or REGISTRY_CSV, deploy_csv or DEPLOY_CSV
    pairs = {row.get("setting", ""): row.get("value", "") for row in read_rows(registry_csv)}
    missing = [name for name in REGISTRY_SETTINGS if not pairs.get(name)]
    if missing:
        raise DeployError("settings", f"{registry_csv.name} has no value for {missing}")
    # serving_image is <repository>/<image>, no tag: the images of this service live in the same repository.
    repository = pairs["serving_image"].rsplit("/", 1)[0]

    rows = read_rows(deploy_csv)
    images = [row["name"] for row in rows if row.get("kind") == "image"]
    services = {row["name"]: row.get("routes", "") for row in rows if row.get("kind") == "service"}
    if len(images) != 1 or not images[0]:
        raise DeployError("settings", f"{deploy_csv.name} needs exactly one image row")
    if not services or not all(services.values()):
        raise DeployError("settings", f"{deploy_csv.name} needs a service row with a name and routes")
    return Settings(project=pairs["project"], region=pairs["region"], model_display_name=pairs["model_display_name"],
                    model_bucket=pairs["model_bucket"], alias_production=pairs["alias_production"],
                    repository=repository, image_name=images[0], services=services)


# ---- running programs (a fake in the tests)


class Runner:
    """Runs a program. capture=False lets its output go to the terminal (a build is long)."""

    def run(self, argv: list[str], input: str | None = None, capture: bool = True,
            env: dict[str, str] | None = None) -> tuple[int, str]:
        """env, when given, is added to the process environment for this one call."""
        try:
            result = subprocess.run(argv, input=input, text=True, check=False,
                                    env=None if env is None else {**os.environ, **env},
                                    stdout=subprocess.PIPE if capture else None,
                                    stderr=subprocess.STDOUT if capture else None)
        except OSError as exc:
            return 127, f"cannot run {argv[0]}: {exc}"
        return result.returncode, result.stdout or ""


# ---- Google APIs over an authorized session


def default_session(quota_project: str):
    """An authorized requests session from Application Default Credentials: the CI identity from Workload
    Identity Federation on a runner, `gcloud auth application-default login` on a laptop. No key file. An
    end-user login needs a quota project (project 2); the CI identity gets none (vertex_client.py, #111)."""
    import warnings

    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials as UserCredentials

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*without a quota project", category=UserWarning)
            credentials, _ = google.auth.default(scopes=list(VERTEX_SCOPES))
    except DefaultCredentialsError as exc:
        raise DeployError("credentials", f"no Google credentials: {exc}") from None
    if isinstance(credentials, UserCredentials):
        credentials = credentials.with_quota_project(quota_project)
    return AuthorizedSession(credentials)


def access_token(session) -> str:
    """The session's current access token, refreshed when it has none."""
    credentials = session.credentials
    if not getattr(credentials, "token", None):
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
    return credentials.token


def message_of(response) -> str:
    try:
        return response.json().get("error", {}).get("message", "") or response.text[:300]
    except ValueError:
        return response.text[:300]


class Api:
    """JSON calls through the session. Any refusal is a DeployError for the step that made the call."""

    def __init__(self, session, step: str = "api"):
        self.session = session
        self.step = step

    def call(self, method: str, url: str, raw: bool = False, **kwargs):
        import requests

        try:
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as exc:
            raise DeployError(self.step, f"{method} {url} did not complete: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise DeployError(self.step, f"{method} {url} failed ({response.status_code}): {message_of(response)}")
        if raw:
            return response.content
        return response.json() if response.content else {}


# ---- step 2: the registry version (the calls of vertex_client.py, cut to what a deploy needs)


@dataclass(frozen=True)
class Version:
    version_id: str
    folder: str  # the model bucket folder holding its files


def pages(api: Api, url: str, key: str, params: dict | None = None) -> list[dict]:
    """Every item of a list call, following nextPageToken until it is empty."""
    params, items = dict(params or {}), []
    while True:
        body = api.call("GET", url, params=params)
        items.extend(body.get(key, []))
        token = body.get("nextPageToken")
        if not token:
            return items
        params["pageToken"] = token


def resolve_version(settings: Settings, api: Api, wanted: str | None) -> Version:
    base = f"https://{settings.region}-aiplatform.googleapis.com/v1"
    parent = f"projects/{settings.project}/locations/{settings.region}"
    found = pages(api, f"{base}/{parent}/models", "models",
                  {"filter": f'display_name="{settings.model_display_name}"'})
    if len(found) != 1:
        raise DeployError("resolve", f"{len(found)} registry models are named {settings.model_display_name!r}; expected 1")
    model_name = found[0]["name"]
    versions = pages(api, f"{base}/{model_name}:listVersions", "models")
    if wanted is None:
        held = [v for v in versions if settings.alias_production in v.get("versionAliases", [])]
        if len(held) != 1:
            raise DeployError("resolve", f"{len(held)} versions hold the alias {settings.alias_production!r}; expected 1")
        chosen = held[0]
    else:
        matching = [v for v in versions if str(v.get("versionId")) == wanted]
        if not matching:
            raise DeployError("resolve", f"the registry model has no version {wanted!r}")
        chosen = matching[0]
    version_id = str(chosen["versionId"])
    if not version_id.isdigit():
        raise DeployError("resolve", f"version id {version_id!r} is not a number")
    description = chosen.get("versionDescription", "") or chosen.get("description", "")
    where = FILES_FOLDER.search(description)
    if where is None:
        raise DeployError("resolve", f"version {version_id} does not say where its files are")
    if where.group(1) != settings.model_bucket:
        raise DeployError("resolve", f"version {version_id} files are in gs://{where.group(1)}, not the model bucket")
    return Version(version_id=version_id, folder=where.group(2))


# ---- steps 3 and 4: download, verify, prepare


def download(api: Api, bucket: str, name: str) -> bytes:
    url = f"{STORAGE}/b/{quote(bucket, safe='')}/o/{quote(name, safe='')}"
    return api.call("GET", url, raw=True, params={"alt": "media"})


def fetch_and_verify(api: Api, settings: Settings, version: Version) -> dict[str, bytes]:
    """Every file metadata.json lists, each checked against its sha256, plus metadata.json itself. Any mismatch
    stops the script: nothing is built from a file that does not match."""
    metadata_bytes = download(api, settings.model_bucket, f"{version.folder}/metadata.json")
    try:
        metadata = json.loads(metadata_bytes)
        files = metadata["files"]
    except (ValueError, KeyError, TypeError):
        raise DeployError("download", "metadata.json is not JSON with a files table") from None
    if not isinstance(files, dict) or "model.joblib" not in files:
        raise DeployError("download", "metadata.json lists no sha256 for model.joblib")
    out = {"metadata.json": metadata_bytes}
    for name, expected in sorted(files.items()):
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise DeployError("download", f"metadata.json lists a file name that is not a plain name: {name!r}")
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise DeployError("download", f"metadata.json gives {name} no sha256:<64 hex> value")
        data = download(api, settings.model_bucket, f"{version.folder}/{name}")
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise DeployError("verify", f"{name} does not match its sha256 (metadata.json {expected}, got {actual})")
        out[name] = data
    return out


def write_model_folder(folder: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("model.joblib", "metadata.json"):
        (folder / name).write_bytes(files[name])


# ---- step 1: the tree


def git_commit(runner: Runner) -> str:
    code, out = runner.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    commit = out.strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DeployError("tree", "cannot read the git commit (is this a git checkout?)")
    return commit


def dirty_paths(runner: Runner) -> list[str]:
    code, out = runner.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", *CLEAN_PATHS])
    if code != 0:
        raise DeployError("tree", "git status failed")
    return [line for line in out.splitlines() if line.strip()]


# ---- steps 6 and 7: build and push


def image_ref(settings: Settings) -> str:
    return f"{settings.repository}/{settings.image_name}"


def build_image(runner: Runner, settings: Settings, version: Version, folder: Path, commit: str) -> str:
    tag = f"{image_ref(settings)}:v{version.version_id}-{commit[:12]}"
    code, _ = runner.run(["docker", "build", "-f", str(ROOT / "Serving" / "Dockerfile"), "--target", "runtime",
                          "--platform", "linux/amd64", "--provenance=false",
                          "--build-context", f"model={folder}", "--build-arg", f"MODEL_VERSION={version.version_id}",
                          "-t", tag, str(ROOT)], capture=False)
    if code != 0:
        raise DeployError("build", "docker build failed")
    return tag


def push_image(runner: Runner, session, settings: Settings, tag: str) -> str:
    """Log in with a short-lived access token on stdin (never in argv or a log), push, read the digest.

    The four docker calls run with DOCKER_CONFIG set to a fresh temporary folder, deleted after, so the user's
    ~/.docker config and credential helpers are never read or changed."""
    registry = settings.repository.split("/", 1)[0]
    with tempfile.TemporaryDirectory(prefix="brt-docker-") as config:
        env = {"DOCKER_CONFIG": config}
        code, _ = runner.run(["docker", "login", "-u", "oauth2accesstoken", "--password-stdin", registry],
                             input=access_token(session), env=env)
        if code != 0:
            raise DeployError("push", f"docker login to {registry} failed")
        try:
            code, _ = runner.run(["docker", "push", tag], capture=False, env=env)
            if code != 0:
                raise DeployError("push", f"docker push {tag} failed")
            code, out = runner.run(["docker", "image", "inspect", "--format",
                                    "{{range .RepoDigests}}{{println .}}{{end}}", tag], env=env)
        finally:
            runner.run(["docker", "logout", registry], env=env)
    prefix = image_ref(settings) + "@"
    digests = [line.strip().removeprefix(prefix) for line in out.splitlines() if line.strip().startswith(prefix)]
    if code != 0 or not digests or not DIGEST.fullmatch(digests[0]):
        raise DeployError("push", f"no registry digest for {tag} after the push")
    return digests[0]


# ---- step 8: deploy


def patch_body(service: dict, image: str, version_id: str, commit: str) -> dict:
    """The change to a service. A repeated field in an update mask is replaced whole, so `containers` carries
    the service's own container as the API returned it with only `image` changed, and `labels` its own template
    labels plus the two this deploy sets. Everything Terraform set (env, resources, probe, scaling, identity)
    is therefore sent back as it is."""
    template = service.get("template") or {}
    containers = [dict(c) for c in template.get("containers") or []]
    if len(containers) != 1:
        raise DeployError("deploy", f"service has {len(containers)} containers; expected 1")
    containers[0]["image"] = image
    labels = dict(template.get("labels") or {})
    labels.update({"model-version": version_id, "git-commit": commit[:12]})
    return {"template": {"labels": labels, "containers": containers}}


PATCH_MASK = "template.labels,template.containers"


def wait_operation(api: Api, operation: dict, step: str, sleep=time.sleep) -> dict:
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    while not operation.get("done"):
        if time.monotonic() > deadline:
            raise DeployError(step, f"operation {operation.get('name')} not done after {OPERATION_TIMEOUT_SECONDS} s")
        sleep(POLL_SECONDS)
        operation = api.call("GET", f"{RUN_API}/{operation['name']}")
    if "error" in operation:
        raise DeployError(step, f"the revision failed: {operation['error'].get('message', operation['error'])}")
    return operation.get("response", {})


def failed_condition(api: Api, revision: str) -> str:
    try:
        conditions = api.call("GET", f"{RUN_API}/{revision}").get("conditions", [])
    except DeployError:
        return "(the revision's conditions could not be read)"
    bad = [c.get("message", "") for c in conditions if c.get("state") != "CONDITION_SUCCEEDED" and c.get("message")]
    return "; ".join(bad) or "(no condition message)"


def deploy_service(api: Api, settings: Settings, name: str, routes: str, digest: str, version_id: str,
                   commit: str, sleep=time.sleep) -> dict:
    path = f"{RUN_API}/projects/{settings.project}/locations/{settings.region}/services/{name}"
    service = api.call("GET", path)
    live_routes = {e.get("name"): e.get("value") for c in (service.get("template") or {}).get("containers", [])
                   for e in c.get("env", [])}.get("SERVING_ROUTES")
    if live_routes != routes:
        raise DeployError("deploy", f"{name} has SERVING_ROUTES={live_routes!r}, deploy.csv says {routes!r}; "
                                    "run the Terraform plan for Deployment/terraform-project2")
    body = patch_body(service, f"{image_ref(settings)}@{digest}", version_id, commit)
    operation = api.call("PATCH", path, params={"updateMask": PATCH_MASK}, json=body)
    wait_operation(api, operation, "deploy", sleep)
    after = api.call("GET", path)
    ready, created = after.get("latestReadyRevision", ""), after.get("latestCreatedRevision", "")
    if not ready or ready != created:
        raise DeployError("deploy", f"{name} revision {created or '(none)'} is not ready: "
                                    f"{failed_condition(api, created) if created else 'no revision was created'}")
    return {"service": name, "url": after.get("uri", ""), "revision": ready.rsplit("/", 1)[-1],
            "version": version_id, "digest": digest}


# ---- the run


def run(args, runner: Runner | None = None, session_factory=default_session, out=print) -> int:
    runner = runner or Runner()
    step = "settings"
    try:
        settings = load_settings()
        step = "tree"
        commit = git_commit(runner)
        dirty = dirty_paths(runner)
        if dirty:
            detail = "\n".join(f"  {line}" for line in dirty)
            if not args.dry_run:
                raise DeployError("tree", f"uncommitted changes under {', '.join(CLEAN_PATHS)}; the image must come "
                                          f"from a commit:\n{detail}")
            print(f"warning: uncommitted changes under {', '.join(CLEAN_PATHS)} (a real deploy stops here):\n{detail}",
                  file=sys.stderr)

        step = "credentials"
        session = session_factory(settings.project)
        step = "resolve"
        version = resolve_version(settings, Api(session, "resolve"), args.version)
        step = "download"
        files = fetch_and_verify(Api(session, "download"), settings, version)

        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(args.keep) if args.keep else Path(scratch) / "model"
            write_model_folder(folder, files)
            out(f"version {version.version_id}, files gs://{settings.model_bucket}/{version.folder}/, commit {commit[:12]}")
            out(f"model folder {folder}")
            for name in sorted(files):
                note = "  (carries the list, not on it)" if name == "metadata.json" else "  (verified)"
                out(f"  {name}  sha256:{hashlib.sha256(files[name]).hexdigest()}{note}")
            if args.dry_run:
                out("dry run: nothing built, pushed or deployed")
                return 0

            step = "build"
            tag = build_image(runner, settings, version, folder, commit)
            step = "push"
            digest = push_image(runner, session, settings, tag)
            step = "deploy"
            api = Api(session, "deploy")
            results = [deploy_service(api, settings, name, routes, digest, version.version_id, commit)
                       for name, routes in settings.services.items()]
        for result in results:
            out(f"{result['service']}: {result['url']} revision {result['revision']} "
                f"model version {result['version']} image {image_ref(settings)}@{result['digest']}")
        return 0
    except DeployError as exc:
        print(f"deploy failed at step {exc.step}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # any other failure still ends with one line naming the step
        print(f"deploy failed at step {step}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", help="the registry version id to deploy; default: the production alias")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve, download and verify, print the version and each sha256, then stop")
    parser.add_argument("--keep", metavar="DIR", help="put model.joblib and metadata.json in DIR and keep them")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse()))
