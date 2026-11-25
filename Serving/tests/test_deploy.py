"""Serving/scripts/deploy.py (#161, ADR-038), with a fake Google session and a fake program runner: no network, no
docker, no git. The world below is what the registry, the model bucket and the two Cloud Run services answer."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

import deploy
import pytest

TOKEN = "ya29.SECRET-ACCESS-TOKEN-0123456789"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "a" * 64
MODEL_BYTES = b"not a real pickle, only bytes that are hashed"
OTHER = {"run.json": b'{"run": 1}', "metrics.json": b'{"f1": 0.5}'}


class Response:
    def __init__(self, body=None, status=200, content=None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


class Credentials:
    token = TOKEN


class World:
    """The fake Google: a registry with versions, a bucket of files and two services."""

    def __init__(self, settings: deploy.Settings):
        self.settings = settings
        self.files: dict[str, bytes] = {}
        self.versions = []
        self.add_version("7", ["production"], "surge-forecast/run-a/abc", {"model.joblib": MODEL_BYTES, **OTHER})
        self.add_version("8", ["candidate"], "surge-forecast/run-b/def", {"model.joblib": b"a later model", **OTHER})
        self.services = {name: self.service(name, routes) for name, routes in settings.services.items()}
        self.page_size = 0  # 0: everything in one answer
        self.operation_error: dict[str, str] = {}
        self.revision_ready = True
        self.calls: list[tuple[str, str, dict]] = []

    def add_version(self, version_id, aliases, folder, files):
        listed = {name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in files.items()}
        for name, data in files.items():
            self.files[f"{folder}/{name}"] = data
        self.files[f"{folder}/metadata.json"] = json.dumps({"files": listed, "threshold": 0.3}).encode()
        self.versions.append({"versionId": version_id, "versionAliases": aliases, "versionDescription":
                              f"x run, files gs://{self.settings.model_bucket}/{folder}/ (metadata.json)"})

    def service(self, name, routes):
        base = f"projects/{self.settings.project}/locations/{self.settings.region}/services"
        return {"name": f"{base}/{name}",
                "uri": f"https://{name}-123.a.run.app", "latestReadyRevision": f"{base}/{name}/revisions/{name}-00001-abc",
                "latestCreatedRevision": f"{base}/{name}/revisions/{name}-00001-abc",
                "template": {"serviceAccount": "brt-serving@p.iam.gserviceaccount.com", "timeout": "30s",
                             "labels": {"keep": "me"},
                             "scaling": {"maxInstanceCount": 2},
                             "containers": [{"image": "us-docker.pkg.dev/cloudrun/container/hello",
                                             "env": [{"name": "SERVING_ROUTES", "value": routes}],
                                             "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                                             "startupProbe": {"httpGet": {"path": "/health"}}}]}}

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))  # a snapshot: the caller reuses its params dict
        if "aiplatform.googleapis.com" in url:
            if url.endswith("/models"):
                return self.page([{"name": "projects/p/locations/r/models/9"}], kwargs)
            return self.page(self.versions, kwargs)
        if "storage.googleapis.com" in url:
            name = unquote(re.search(r"/o/([^?]+)", url).group(1))
            return Response(content=self.files[name]) if name in self.files else Response({"error": {"message": "no"}}, 404)
        if "run.googleapis.com" in url:
            return self.run_api(method, url, kwargs)
        raise AssertionError(f"unexpected call {method} {url}")

    def page(self, items, kwargs):
        """One page of a list answer when page_size is set; the token is the index of the next item."""
        if not self.page_size:
            return Response({"models": items})
        start = int((kwargs.get("params") or {}).get("pageToken", 0))
        body = {"models": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            body["nextPageToken"] = str(start + self.page_size)
        return Response(body)

    def run_api(self, method, url, kwargs):
        if "/operations/" in url:
            raise AssertionError("operations are returned done")
        name = url.rsplit("/", 1)[-1]
        if name.startswith("brt-") and "-0000" in name:  # a revision
            return Response({"conditions": [{"state": "CONDITION_FAILED", "message": "model failed its sha256 check"}]})
        if method == "GET":
            return Response(self.services[name])
        assert method == "PATCH"
        body = kwargs["json"]
        if name in self.operation_error:
            return Response({"name": "projects/p/locations/r/operations/1", "done": True,
                             "error": {"message": self.operation_error[name]}})
        service = self.services[name]
        service["template"] = {**service["template"], **body["template"]}
        revision = f"{service['name']}/revisions/{name}-00002-xyz"
        service["latestCreatedRevision"] = revision
        service["latestReadyRevision"] = revision if self.revision_ready else service["latestReadyRevision"]
        return Response({"name": "projects/p/locations/r/operations/1", "done": True, "response": {}})

    # the session interface
    credentials = Credentials()


class Runner:
    def __init__(self, settings, dirty="", inspect_digest=DIGEST, fail=()):
        self.settings = settings
        self.dirty, self.inspect_digest, self.fail = dirty, inspect_digest, fail
        self.calls: list[tuple[list[str], str | None]] = []
        self.envs: list[dict | None] = []
        self.config_dirs: list[tuple[str, bool]] = []  # (DOCKER_CONFIG, existed during the call)

    def run(self, argv, input=None, capture=True, env=None):
        self.calls.append((list(argv), input))
        self.envs.append(env)
        if env and "DOCKER_CONFIG" in env:
            self.config_dirs.append((env["DOCKER_CONFIG"], Path(env["DOCKER_CONFIG"]).is_dir()))
        if argv[0] == "git" and "rev-parse" in argv:
            return 0, COMMIT + "\n"
        if argv[0] == "git":
            return 0, self.dirty
        if argv[:2] == ["docker", "image"]:
            ref = f"{deploy.image_ref(self.settings)}@{self.inspect_digest}\n" if self.inspect_digest else ""
            return 0, ref
        verb = argv[1]
        return (1, "") if verb in self.fail else (0, "")

    def docker(self, verb):
        return [argv for argv, _ in self.calls if argv[0] == "docker" and argv[1] == verb]


@pytest.fixture
def settings():
    return deploy.load_settings()


@pytest.fixture
def world(settings):
    return World(settings)


def go(world, runner, *argv, tmp_path=None):
    args = deploy.parse(list(argv))
    return deploy.run(args, runner=runner, session_factory=lambda project: world)


def test_a_clean_run_deploys_both_services_and_prints_each(world, settings, tmp_path, capsys):
    runner = Runner(settings)
    assert go(world, runner, "--keep", str(tmp_path / "m")) == 0
    out = capsys.readouterr().out
    for name in settings.services:
        assert f"{name}: https://{name}-123.a.run.app revision {name}-00002-xyz model version 7 image " \
               f"{deploy.image_ref(settings)}@{DIGEST}" in out
    build = runner.docker("build")[0]
    assert f"model={tmp_path / 'm'}" in build and "MODEL_VERSION=7" in build and "runtime" in build
    assert build[build.index("-t") + 1] == f"{deploy.image_ref(settings)}:v7-{COMMIT[:12]}"
    assert sorted(p.name for p in (tmp_path / "m").iterdir()) == ["metadata.json", "model.joblib"]


def test_a_dirty_tree_is_refused_before_any_cloud_call(world, settings, capsys):
    runner = Runner(settings, dirty=" M Serving/src/serving/app.py\n")
    assert go(world, runner) == 2
    err = capsys.readouterr().err
    assert err.count("\n") >= 1 and "deploy failed at step tree" in err and "Serving/src/serving/app.py" in err
    assert world.calls == [] and runner.docker("build") == []


def test_the_dirty_check_covers_what_the_image_copies(world, settings):
    runner = Runner(settings)
    go(world, runner, "--dry-run")
    status = next(argv for argv, _ in runner.calls if argv[0] == "git" and "status" in argv)
    for path in ("Serving", "docs/contracts/schemas", "Frontend", "Model-Development/requirements.lock"):
        assert path in status


def test_a_sha256_mismatch_stops_before_any_build(world, settings, capsys):
    world.files["surge-forecast/run-a/abc/model.joblib"] = b"tampered"
    runner = Runner(settings)
    assert go(world, runner) == 2
    err = capsys.readouterr().err
    assert "deploy failed at step verify" in err and "model.joblib does not match" in err
    assert runner.docker("build") == [] and runner.docker("push") == [] and runner.docker("login") == []


def test_a_listed_file_that_is_missing_stops_before_any_build(world, settings, capsys):
    del world.files["surge-forecast/run-a/abc/run.json"]
    runner = Runner(settings)
    assert go(world, runner) == 2
    assert "deploy failed at step download" in capsys.readouterr().err and runner.docker("build") == []


def test_a_dry_run_builds_nothing_and_prints_the_version_and_each_hash(world, settings, tmp_path, capsys):
    runner = Runner(settings, dirty="?? Serving/scripts/deploy.py\n")  # a dry run warns on a dirty tree
    assert go(world, runner, "--dry-run", "--keep", str(tmp_path / "kept")) == 0
    captured = capsys.readouterr()
    assert "version 7" in captured.out and str(tmp_path / "kept") in captured.out
    assert "sha256:" + hashlib.sha256(MODEL_BYTES).hexdigest() in captured.out
    assert "uncommitted changes" in captured.err
    assert not [argv for argv, _ in runner.calls if argv[0] == "docker"]
    assert (tmp_path / "kept" / "model.joblib").read_bytes() == MODEL_BYTES
    assert [c for c in world.calls if c[0] == "PATCH"] == []


def test_the_patch_holds_only_the_image_by_digest_and_the_two_labels(world, settings):
    before = copy.deepcopy(world.services)
    assert go(world, Runner(settings)) == 0
    patches = [(url, kw) for method, url, kw in world.calls if method == "PATCH"]
    assert len(patches) == len(settings.services)
    for url, kwargs in patches:
        name = url.rsplit("/", 1)[-1]
        assert kwargs["params"] == {"updateMask": "template.labels,template.containers"}
        template = kwargs["json"]["template"]
        assert set(kwargs["json"]) == {"template"} and set(template) == {"labels", "containers"}
        assert template["labels"] == {"keep": "me", "model-version": "7", "git-commit": COMMIT[:12]}
        image = template["containers"][0]["image"]
        assert image == f"{deploy.image_ref(settings)}@{DIGEST}" and ":v" not in image
        # Everything else in the container is what the service already had.
        expected = copy.deepcopy(before[name]["template"]["containers"][0])
        expected["image"] = image
        assert template["containers"] == [expected]


def test_a_failed_revision_exits_2_with_its_message(world, settings, capsys):
    first = next(iter(settings.services))
    world.operation_error[first] = "Revision failed: the container failed the startup probe"
    assert go(world, Runner(settings)) == 2
    err = capsys.readouterr().err
    assert "deploy failed at step deploy" in err and "the container failed the startup probe" in err


def test_a_revision_that_is_created_but_not_ready_prints_its_condition(world, settings, capsys):
    world.revision_ready = False
    assert go(world, Runner(settings)) == 2
    err = capsys.readouterr().err
    assert "is not ready" in err and "model failed its sha256 check" in err


def test_a_service_whose_routes_differ_from_deploy_csv_is_not_patched(world, settings, capsys):
    name = next(iter(settings.services))
    world.services[name]["template"]["containers"][0]["env"] = [{"name": "SERVING_ROUTES", "value": "all"}]
    assert go(world, Runner(settings)) == 2
    assert f"{name} has SERVING_ROUTES='all'" in capsys.readouterr().err
    assert [c for c in world.calls if c[0] == "PATCH"] == []


def test_the_token_is_never_in_argv_or_in_the_output(world, settings, capsys):
    runner = Runner(settings)
    assert go(world, runner) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err
    assert all(TOKEN not in " ".join(argv) for argv, _ in runner.calls)
    logins = [(argv, given) for argv, given in runner.calls if argv[:2] == ["docker", "login"]]
    assert len(logins) == 1 and logins[0][1] == TOKEN and "--password-stdin" in logins[0][0]
    # and a failure's message does not carry it either
    runner = Runner(settings, fail=("push",))
    assert go(world, runner) == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err


def test_the_registry_is_logged_out_even_when_the_push_fails(world, settings, capsys):
    runner = Runner(settings, fail=("push",))
    assert go(world, runner) == 2
    assert "deploy failed at step push" in capsys.readouterr().err and len(runner.docker("logout")) == 1


def test_the_docker_registry_calls_use_a_fresh_config_folder_and_the_build_does_not(world, settings):
    runner = Runner(settings)
    assert go(world, runner) == 0
    by_verb = {}
    for (argv, _), env in zip(runner.calls, runner.envs):
        if argv[0] == "docker":
            verb = " ".join(argv[1:3]) if argv[1] == "image" else argv[1]
            by_verb[verb] = env
    assert set(by_verb) == {"build", "login", "push", "image inspect", "logout"}
    assert by_verb["build"] is None
    folders = {env["DOCKER_CONFIG"] for verb, env in by_verb.items() if verb != "build"}
    assert all(set(by_verb[v]) == {"DOCKER_CONFIG"} for v in ("login", "push", "image inspect", "logout"))
    assert len(folders) == 1
    folder = folders.pop()
    assert Path(folder) != Path.home() / ".docker" and "brt-docker-" in folder
    assert all(existed for _, existed in runner.config_dirs) and len(runner.config_dirs) == 4
    assert not Path(folder).exists()  # deleted after


def test_the_config_folder_is_deleted_when_the_push_fails(world, settings):
    runner = Runner(settings, fail=("push",))
    assert go(world, runner) == 2
    assert runner.config_dirs and not Path(runner.config_dirs[0][0]).exists()


def test_the_build_is_for_amd64_with_no_provenance(world, settings, tmp_path):
    runner = Runner(settings)
    assert go(world, runner, "--keep", str(tmp_path / "m")) == 0
    build = runner.docker("build")[0]
    assert build[build.index("--platform") + 1] == "linux/amd64" and "--provenance=false" in build


def test_no_digest_after_the_push_is_a_failure(world, settings, capsys):
    assert go(world, Runner(settings, inspect_digest="")) == 2
    assert "no registry digest" in capsys.readouterr().err


def test_the_production_alias_on_the_second_page_is_found(world, settings):
    world.versions = [{"versionId": str(n), "versionAliases": [], "versionDescription": "x"} for n in (1, 2)] \
        + [world.versions[0]]
    world.page_size = 2
    runner = Runner(settings)
    assert go(world, runner) == 0
    assert "MODEL_VERSION=7" in runner.docker("build")[0]
    tokens = [kw["params"].get("pageToken") for m, u, kw in world.calls if u.endswith(":listVersions")]
    assert tokens == [None, "2"]


def test_a_version_given_on_the_second_page_is_found(world, settings):
    world.versions = [{"versionId": str(n), "versionAliases": [], "versionDescription": "x"} for n in (1, 2)] \
        + [world.versions[1]]
    world.page_size = 2
    runner = Runner(settings)
    assert go(world, runner, "--version", "8") == 0
    assert "MODEL_VERSION=8" in runner.docker("build")[0]


def test_the_models_list_is_followed_to_its_last_page(world, settings):
    world.page_size = 1
    assert go(world, Runner(settings), "--dry-run") == 0
    assert any(u.endswith("/models") for m, u, kw in world.calls)


def test_a_version_is_taken_when_given(world, settings, capsys):
    runner = Runner(settings)
    assert go(world, runner, "--version", "8") == 0
    assert "MODEL_VERSION=8" in runner.docker("build")[0]
    assert go(world, Runner(settings), "--version", "99") == 2
    assert "has no version '99'" in capsys.readouterr().err


def test_no_production_alias_is_a_failure(world, settings, capsys):
    world.versions[0]["versionAliases"] = []
    assert go(world, Runner(settings)) == 2
    assert "0 versions hold the alias" in capsys.readouterr().err


def test_a_session_that_cannot_be_made_is_a_failure(settings, capsys):
    def no_credentials(project):
        raise deploy.DeployError("credentials", "no Google credentials: none")

    assert deploy.run(deploy.parse(["--dry-run"]), runner=Runner(settings), session_factory=no_credentials) == 2
    assert "deploy failed at step credentials" in capsys.readouterr().err


# ---- settings are files, not constants


REGISTRY = """setting,value,options,note
project,proj-x-9,,
region,europe-west4,,
model_display_name,my-model,,
model_bucket,my-bucket,,
serving_image,europe-west4-docker.pkg.dev/proj-x-9/my-repo/brt-train,,
alias_production,champion,,
"""
DEPLOY = """kind,name,routes,note
image,my-image,,
service,svc-a,model,
service,svc-b,web,
"""


def test_settings_come_from_the_two_csv_files(tmp_path):
    (tmp_path / "registry.csv").write_text(REGISTRY, encoding="utf-8")
    (tmp_path / "deploy.csv").write_text(DEPLOY, encoding="utf-8")
    loaded = deploy.load_settings(tmp_path / "registry.csv", tmp_path / "deploy.csv")
    assert (loaded.project, loaded.region, loaded.model_bucket, loaded.alias_production) == \
        ("proj-x-9", "europe-west4", "my-bucket", "champion")
    assert loaded.repository == "europe-west4-docker.pkg.dev/proj-x-9/my-repo"
    assert loaded.image_name == "my-image" and loaded.services == {"svc-a": "model", "svc-b": "web"}


def test_a_run_uses_the_settings_files_it_is_pointed_at(world, settings, tmp_path, monkeypatch):
    # A different image name in deploy.csv changes the tag the build uses: nothing is a constant in the script.
    text = (deploy.DEPLOY_CSV).read_text(encoding="utf-8").replace("brt-serving", "renamed-image")
    (tmp_path / "deploy.csv").write_text(text, encoding="utf-8")
    monkeypatch.setattr(deploy, "DEPLOY_CSV", tmp_path / "deploy.csv")
    runner = Runner(deploy.load_settings())
    assert go(world, runner) == 0
    tag = runner.docker("build")[0][-2]
    assert "/renamed-image:v7-" in tag


def test_a_missing_setting_is_named(tmp_path):
    (tmp_path / "registry.csv").write_text("setting,value\nproject,proj-x-9\n", encoding="utf-8")
    with pytest.raises(deploy.DeployError, match="model_bucket"):
        deploy.load_settings(tmp_path / "registry.csv", deploy.DEPLOY_CSV)


def test_the_shipped_settings_files_load_and_name_both_services(settings):
    assert settings.services == {"brt-model": "model", "brt-web": "web"}
    assert settings.image_name == "brt-serving" and settings.repository.endswith("/brt-images")
    assert settings.project and settings.region and settings.model_bucket


def test_no_project_id_is_in_the_script():
    text = (deploy.HERE).read_text(encoding="utf-8")
    assert "boston-resilience-twin" not in text
