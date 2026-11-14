"""Tests for the registry push, champion, rollback and re-score (#111, ADR-006).

Every test uses a fake registry client (FakeRegistry): it keeps versions and moves aliases the way
Vertex does (one version per alias; assigning an alias another version holds moves it). No test
reaches project 2, MLflow or the network.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import registry as R  # noqa: E402
import training_data  # noqa: E402
import vertex_client as V  # noqa: E402

MODEL = "projects/p/locations/us-east1/models/42"
DATA = "sha256:" + "a" * 64
GATES = "d" * 64
COMMIT = "b" * 40
MLFLOW_RUN = "c" * 32
RUN_ID = "20261112T200000Z-xgboost"
WINDOWS = {"train": ["2015-01-01", "2023-12-31"], "validation": ["2024-01-01", "2024-12-31"],
           "test": ["2025-01-01", "2026-06-30"]}


# --- fakes and builders ------------------------------------------------------


class FakeRegistry:
    """The calls registry.py makes on vertex_client.VertexClient, kept in memory."""

    def __init__(self, settings, versions: int = 0, deny: str | None = None, fail: str | None = None):
        self.settings = settings
        self.model = MODEL if versions else None
        self.versions: dict[str, dict] = {}
        self.objects: dict[str, bytes] = {}
        self.deny, self.fail, self.calls = deny, fail, []
        self.deny_merges, self.fail_merges = set(), set()  # "<version>:merge:<aliases>" that refuse or fail
        for _ in range(versions):
            self._add({"labels": {"data_version": DATA[7:39], "gates_sha256": GATES[:32]}})

    def _call(self, name: str):
        self.calls.append(name)
        if self.deny == name:
            raise V.NoAccess(f"{name} refused (403)")
        if self.fail == name:
            raise R.RegistryError(f"{name} failed (500)")

    def _add(self, body: dict) -> str:
        version = str(len(self.versions) + 1)
        self.versions[version] = {"versionId": version, "versionAliases": [], **copy.deepcopy(body)}
        return version

    def find_model(self):
        self._call("find_model")
        return self.model

    def upload_file(self, local, object_name):
        self._call("upload_file")
        if object_name in self.objects:
            raise R.RegistryError("already exists")
        self.objects[object_name] = Path(local).read_bytes()
        return f"gs://{self.settings.model_bucket}/{object_name}"

    def download_file(self, object_name, local):
        self._call("download_file")
        Path(local).write_bytes(self.objects[object_name])
        return Path(local)

    def upload_version(self, model, parent):
        self._call("upload_version")
        assert parent == self.model
        self.model = self.model or MODEL
        return self.model, self._add(model)

    def get_version(self, model_name, ref):
        self._call("get_version")
        return copy.deepcopy(self.versions[ref])

    def list_versions(self, model_name):
        self._call("list_versions")
        return copy.deepcopy(list(self.versions.values()))

    def find_alias(self, model_name, alias):
        return next((v for v in self.list_versions(model_name) if alias in v["versionAliases"]), None)

    def merge_aliases(self, model_name, version_id, add=(), remove=()):
        self._call("merge_aliases")
        key = "merge:" + ",".join([*add, *(f"-{alias}" for alias in remove)])
        on = f"{version_id}:{key}"  # the same merge on one version only, e.g. "1:merge:previous"
        if on in self.deny_merges:
            raise V.NoAccess(f"{on} refused (403)")
        if key == self.fail or on in self.fail_merges:
            raise R.RegistryError(f"{on} failed (500)")
        for alias in add:
            for version in self.versions.values():
                if alias in version["versionAliases"]:
                    version["versionAliases"].remove(alias)
            self.versions[version_id]["versionAliases"].append(alias)
        for alias in remove:
            self.versions[version_id]["versionAliases"].remove(alias)

    def aliases(self) -> dict[str, list[str]]:
        return {k: sorted(v["versionAliases"]) for k, v in self.versions.items() if v["versionAliases"]}


@pytest.fixture
def settings():
    return R.load_settings()


@pytest.fixture
def training(tmp_path):
    """training.csv's settings with the run and report folders in tmp_path, and one winner's run."""
    base = training_data.load_settings()
    t = replace(base, runs_dir=tmp_path / "runs", reports_dir=tmp_path / "reports", mlflow_store=tmp_path / "mlruns")
    run_dir = t.runs_dir / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "model.joblib").write_bytes(b"a saved model")
    (run_dir / "metrics.json").write_text("{}")
    (run_dir / "validation_counts.json").write_text("{}")
    write_json(run_dir / "run.json", {"run_id": RUN_ID, "data_version": DATA, "threshold": 0.3,
                                      "git": {"commit": COMMIT, "uncommitted_changes": False},
                                      "settings": {"splits": WINDOWS},
                                      "mlflow": {"run_id": MLFLOW_RUN, "experiment": "brt"}})
    t.reports_dir.mkdir()
    write_json(t.reports_dir / "selection.json", {
        "selection_id": "s1", "winner": "xgboost", "decision": "xgboost wins", "data_version": DATA,
        "ranking": [{"model": "xgboost", "run_folder": RUN_ID, "mlflow_run_id": MLFLOW_RUN,
                     "validation": {"f1": 0.687, "tp": 90}, "test": {"f1": 0.45}}]})
    return t


@pytest.fixture(autouse=True)
def no_mlflow(monkeypatch):
    """The MLflow tag is a pointer, tested on its own below; push tests do not reach MLflow."""
    monkeypatch.setattr(R, "tag_mlflow", lambda *a, **k: None)


def write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def gate(decision="promote", champion=None, **changes) -> dict:
    report = {"decision": decision, "data_version": DATA, "gates_sha256": GATES,
              "fairness_group_sha256": "e" * 64,
              "champion": None if champion is None else {"model_version": champion, "data_version": DATA, "f1": 0.6}}
    return {**report, **changes}


def write_config(tmp_path: Path, **changes) -> Path:
    rows = [line for line in R.DEFAULT_CONFIG.read_text().splitlines()]
    out = [rows[0]]
    for line in rows[1:]:
        name = line.split(",", 1)[0]
        if name in changes:
            if changes[name] is None:
                continue
            parts = line.split(",")
            parts[1] = changes[name]
            line = ",".join(parts)
        out.append(line)
    out += [f"{k},{v},," for k, v in changes.items() if v is not None and k not in {r.split(',')[0] for r in rows}]
    path = tmp_path / "registry.csv"
    path.write_text("\n".join(out) + "\n")
    return path


# --- settings ----------------------------------------------------------------


def test_committed_settings_load(settings):
    assert settings.project == "boston-resilience-twin-p2"
    assert settings.region == "us-east1"
    assert settings.aliases == {"candidate": "candidate", "production": "production", "previous": "previous"}
    assert [p.name for p in settings.dvc_locks] == ["dvc.lock", "dvc.lock"]
    assert all(p.is_absolute() for p in settings.dvc_locks)


def test_registry_config_is_outside_the_training_stage_folder():
    # Model-Development/dvc.yaml's select stage depends on all of Model-Development/config
    assert R.DEFAULT_CONFIG.parent.name == "registry"


@pytest.mark.parametrize("changes, message", [
    ({"alias_production": "default"}, "cannot be 'default'"),
    ({"alias_previous": "production"}, "aliases must differ"),
    ({"alias_candidate": "candidate-"}, "not a valid alias"),
    ({"alias_candidate": "1abc"}, "not a valid alias"),
    ({"region": "US East"}, "region is not a valid name"),
    ({"project": "P"}, "project is not a valid name"),
    ({"model_bucket": "-bad"}, "model_bucket is not a valid name"),
    ({"serving_image": "us-east1-docker.pkg.dev/p/r/i:latest"}, "no tag or digest"),
    ({"serving_image": "us-east1-docker.pkg.dev/p/r/i@sha256:" + "0" * 64}, "no tag or digest"),
    ({"dvc_locks": ";"}, "dvc_locks needs at least one path"),
    ({"region": ""}, "with no value"),
    ({"region": None}, "missing setting"),
    ({"extra": "1"}, "unknown setting"),
])
def test_bad_settings_fail_naming_the_setting(tmp_path, changes, message):
    with pytest.raises(R.RegistryError, match=message):
        R.load_settings(write_config(tmp_path, **changes))


def test_duplicate_setting_fails(tmp_path):
    path = tmp_path / "registry.csv"
    path.write_text(R.DEFAULT_CONFIG.read_text() + "region,us-east4,,\n")
    with pytest.raises(R.RegistryError, match="duplicated"):
        R.load_settings(path)


def test_relative_lock_paths_resolve_from_root(tmp_path):
    loaded = R.load_settings(write_config(tmp_path, dvc_locks="a/dvc.lock;b/dvc.lock"), root=tmp_path)
    assert loaded.dvc_locks == (tmp_path / "a/dvc.lock", tmp_path / "b/dvc.lock")


# --- push --------------------------------------------------------------------


def test_push_registers_the_winner_as_candidate(settings, training):
    client = FakeRegistry(settings)
    record = R.push(settings, training, client, gate(), "sha256:" + "f" * 64)
    assert record["version"] == f"{MODEL}@1" and record["alias"] == "candidate"
    assert client.aliases() == {"1": ["candidate"]}
    assert client.calls[0] == "find_model"  # the access check comes before anything is written
    folder = f"surge-forecast/{RUN_ID}/{MLFLOW_RUN}"
    assert sorted(client.objects) == sorted(f"{folder}/{n}" for n in (
        "model.joblib", "run.json", "metrics.json", "validation_counts.json", "selection.json",
        "gate_report.json", "metadata.json"))
    metadata = json.loads(client.objects[f"{folder}/metadata.json"])
    assert metadata["data_version"] == DATA and metadata["git_commit"] == COMMIT
    assert metadata["mlflow_run_id"] == MLFLOW_RUN
    assert metadata["training_image_digest"] == "sha256:" + "f" * 64
    assert metadata["gate"]["gates_sha256"] == GATES
    assert metadata["windows"]["test"] == {"start": "2025-01-01", "end": "2026-06-30"}
    assert metadata["files"]["model.joblib"] == "sha256:" + hashlib.sha256(b"a saved model").hexdigest()
    body = client.versions["1"]
    assert body["containerSpec"]["imageUri"].endswith("brt-train@sha256:" + "f" * 64)
    assert "artifactUri" not in body  # Vertex would copy the folder; nothing is served from Vertex
    assert body["labels"]["data_version"] == "a" * 32 and body["labels"]["gates_sha256"] == "d" * 32
    assert f"files gs://{settings.model_bucket}/{folder}/ (metadata.json)" in body["versionDescription"]


def test_push_without_digest_names_the_bare_image(settings, training):
    client = FakeRegistry(settings)
    R.push(settings, training, client, gate("keep_champion"))
    assert client.versions["1"]["containerSpec"]["imageUri"] == settings.serving_image
    assert "image_digest" not in client.versions["1"]["labels"]


def test_push_is_a_new_version_of_the_existing_model(settings, training):
    client = FakeRegistry(settings, versions=2)
    record = R.push(settings, training, client, gate())
    assert record["version_id"] == "3"


def test_no_access_at_the_first_call_is_a_skip(settings, training):
    client = FakeRegistry(settings, deny="find_model")
    with pytest.raises(V.NoAccess):
        R.push(settings, training, client, gate())
    assert client.objects == {}


def test_refusal_after_the_access_check_is_an_error(settings, training):
    client = FakeRegistry(settings, deny="upload_version")
    with pytest.raises(R.RegistryError, match="after the access check") as caught:
        R.push(settings, training, client, gate())
    assert not isinstance(caught.value, V.NoAccess)


def test_a_push_that_failed_after_uploading_is_not_rerun_over_its_files(settings, training):
    client = FakeRegistry(settings, deny="upload_version")
    with pytest.raises(R.RegistryError, match="after the access check"):
        R.push(settings, training, client, gate())
    uploaded = dict(client.objects)
    assert uploaded and client.versions == {}  # files in the bucket, no version (runbook: recovery)
    client.deny = None
    with pytest.raises(R.RegistryError, match="already exists"):  # the bucket answers 412: never overwritten
        R.push(settings, training, client, gate())
    assert client.objects == uploaded and client.versions == {}


def test_rescore_refusal_after_the_access_check_is_an_error(settings, training, scored):
    client = FakeRegistry(settings)
    champion_in_bucket(settings, client)
    client.deny = "download_file"
    with pytest.raises(R.RegistryError, match="after the access check") as caught:
        R.rescore(settings, training, client)
    assert not isinstance(caught.value, V.NoAccess)  # not a null champion: gates would skip the comparison


@pytest.mark.parametrize("report, message", [
    (gate("blocked"), "the gates decided 'blocked'"),
    (gate("error"), "the gates decided 'error'"),
    (gate(gates_sha256="nope"), "no gates_sha256"),
    (gate(data_version="sha256:" + "0" * 64), "data versions differ"),
])
def test_push_refuses_bad_gate_reports(settings, training, report, message):
    with pytest.raises(R.RegistryError, match=message):
        R.build_push(settings, training, report)


def test_push_refuses_a_bad_digest(settings, training):
    with pytest.raises(R.RegistryError, match="--image-digest"):
        R.build_push(settings, training, gate(), "latest")


def test_push_refuses_no_winner(settings, training):
    write_json(training.reports_dir / "selection.json", {"winner": None, "decision": "no winner: too weak"})
    with pytest.raises(R.RegistryError, match="no winner: too weak"):
        R.build_push(settings, training, gate())


def test_push_refuses_a_missing_run_file(settings, training):
    (training.runs_dir / RUN_ID / "model.joblib").unlink()
    with pytest.raises(R.RegistryError, match="missing \\['model.joblib'\\]"):
        R.build_push(settings, training, gate())


def _edit_run(training, **changes):
    path = training.runs_dir / RUN_ID / "run.json"
    write_json(path, {**json.loads(path.read_text()), **changes})


def test_push_refuses_a_dirty_tree_unless_a_dry_run(settings, training, capsys):
    _edit_run(training, git={"commit": COMMIT, "uncommitted_changes": True})
    with pytest.raises(R.RegistryError, match="uncommitted changes"):
        R.build_push(settings, training, gate())
    assert R.build_push(settings, training, gate(), allow_dirty=True)["metadata"]["git_commit"] == COMMIT
    assert "a real push stops here" in capsys.readouterr().err


def test_push_takes_the_commit_from_github_sha(settings, training, monkeypatch):
    _edit_run(training, git={"commit": None, "uncommitted_changes": None})
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    with pytest.raises(R.RegistryError, match="no git commit"):
        R.build_push(settings, training, gate())
    monkeypatch.setenv("GITHUB_SHA", "e" * 40)
    assert R.build_push(settings, training, gate())["metadata"]["git_commit"] == "e" * 40


def test_push_refuses_missing_windows(settings, training):
    _edit_run(training, settings={"splits": {"train": WINDOWS["train"]}})
    with pytest.raises(R.RegistryError, match="settings.splits"):
        R.build_push(settings, training, gate())


def test_label_values_are_never_cut_short():
    assert R._label("XGBoost") == "xgboost"
    with pytest.raises(R.RegistryError, match="longer than 63"):
        R._label("x" * 64)


# --- promote and roll back ---------------------------------------------------


def pushed(version: str) -> dict:
    return {"model_name": MODEL, "version_id": version, "metadata": {"data_version": DATA}}


def promote(settings, client, version: str, report: dict) -> dict:
    """R.promote on a version pushed with this gate report (its gate_report label, as the push sets it)."""
    client.versions[version].setdefault("labels", {})["gate_report"] = R._hash_label(R.gate_report_sha256(report))
    return R.promote(settings, client, pushed(version), report)


def test_the_first_version_becomes_champion(settings):
    client = FakeRegistry(settings, versions=1)
    record = promote(settings, client, "1", gate())
    assert record["decision"] == "promote" and record["champion"] == "1" and record["previous"] is None
    assert client.aliases() == {"1": ["production"]}


def test_a_better_challenger_becomes_champion_and_the_old_one_previous(settings):
    client = FakeRegistry(settings, versions=2)
    promote(settings, client, "1", gate())
    record = promote(settings, client, "2", gate("promote", champion="1"))
    assert record["champion_before"] == "1" and record["previous"] == "1"
    assert client.aliases() == {"1": ["previous"], "2": ["production"]}


def test_a_weaker_challenger_leaves_the_champion(settings):
    client = FakeRegistry(settings, versions=2)
    promote(settings, client, "1", gate())
    record = promote(settings, client, "2", gate("keep_champion", champion="1"))
    assert record["decision"] == "keep_champion" and record["champion"] == "1"
    assert client.aliases() == {"1": ["production"]}


@pytest.mark.parametrize("report, message", [
    (gate("promote"), "has no champion block"),
    (gate("promote", champion="7"), "but version 1 holds production"),
    ({**gate("promote", champion="1"), "champion": {"model_version": "1", "data_version": "sha256:" + "0" * 64}},
     "not re-scored on the challenger's data version"),
    (gate("blocked", champion="1"), "the gates decided 'blocked'"),
    (gate("promote", champion="1", data_version="sha256:" + "0" * 64), "is not the pushed version's"),
])
def test_promote_refuses_a_comparison_with_the_wrong_champion(settings, report, message):
    client = FakeRegistry(settings, versions=2)
    promote(settings, client, "1", gate())
    with pytest.raises(R.RegistryError, match=message):
        promote(settings, client, "2", report)
    assert client.aliases() == {"1": ["production"]}


def test_promote_refuses_a_champion_block_with_no_champion(settings):
    with pytest.raises(R.RegistryError, match="no version holds production"):
        promote(settings, FakeRegistry(settings, versions=1), "1", gate("promote", champion="1"))


def test_promote_refuses_a_version_with_other_stamps(settings):
    client = FakeRegistry(settings, versions=1)
    with pytest.raises(R.RegistryError, match="does not carry this gate report's values"):
        promote(settings, client, "1", gate(gates_sha256="9" * 64))


def test_promote_refuses_the_version_already_in_production(settings):
    client = FakeRegistry(settings, versions=1)
    promote(settings, client, "1", gate())
    with pytest.raises(R.RegistryError, match="already holds production"):
        promote(settings, client, "1", gate("promote", champion="1"))


@pytest.mark.parametrize("had_previous", [False, True])
def test_a_failed_production_move_puts_previous_back(settings, had_previous):
    client = FakeRegistry(settings, versions=3)
    promote(settings, client, "1", gate())
    promote(settings, client, "2", gate("promote", champion="1"))
    if not had_previous:
        client.merge_aliases(MODEL, "1", remove=("previous",))
    before = client.aliases()
    client.fail = "merge:production"
    with pytest.raises(R.RegistryError, match="previous was put back"):
        promote(settings, client, "3", gate("promote", champion="2"))
    assert client.aliases() == before


@pytest.mark.parametrize("had_previous", [False, True])
@pytest.mark.parametrize("restore", ["fails", "refused"])
def test_a_failed_restore_is_an_error_naming_both_failures(settings, had_previous, restore):
    client = FakeRegistry(settings, versions=3)
    promote(settings, client, "1", gate())
    promote(settings, client, "2", gate("promote", champion="1"))
    if not had_previous:
        client.merge_aliases(MODEL, "1", remove=("previous",))
    client.fail = "merge:production"
    key = "1:merge:previous" if had_previous else "2:merge:-previous"  # the restore, not the first move
    (client.fail_merges if restore == "fails" else client.deny_merges).add(key)
    with pytest.raises(R.RegistryError) as caught:
        promote(settings, client, "3", gate("promote", champion="2"))
    assert not isinstance(caught.value, V.NoAccess)  # a refused restore is not a skip
    message = str(caught.value)
    assert "3:merge:production failed" in message and f"{key} " in message and "Fix by hand" in message
    assert f"should be on {'version 1' if had_previous else 'no version'}" in message
    assert client.aliases()["2"] == ["previous", "production"]  # the state the message describes


def test_a_refusal_after_the_access_check_in_promote_is_an_error(settings):
    client = FakeRegistry(settings, versions=2)
    promote(settings, client, "1", gate())
    client.deny = "list_versions"
    with pytest.raises(R.RegistryError, match="after the access check") as caught:
        promote(settings, client, "2", gate("promote", champion="1"))
    assert not isinstance(caught.value, V.NoAccess)
    client.deny = "get_version"  # the first call: a skip for the command line
    with pytest.raises(V.NoAccess):
        promote(settings, client, "2", gate("promote", champion="1"))


def test_promote_refuses_a_gate_report_other_than_the_pushed_one(settings):
    client = FakeRegistry(settings, versions=1)
    client.versions["1"]["labels"]["gate_report"] = R._hash_label(R.gate_report_sha256(gate(reason="pushed")))
    with pytest.raises(R.RegistryError, match="does not carry this gate report's values.*gate_report"):
        R.promote(settings, client, pushed("1"), gate(reason="another"))
    assert client.aliases() == {}


def test_cli_promote_exits_2_when_the_restore_is_refused(cli, settings, tmp_path, capsys):
    for _ in range(3):
        cli._add({"labels": {"data_version": DATA[7:39], "gates_sha256": GATES[:32]}})
    cli.model = MODEL
    promote(settings, cli, "1", gate())
    promote(settings, cli, "2", gate("promote", champion="1"))
    report = gate("promote", champion="2")
    cli.versions["3"]["labels"]["gate_report"] = R._hash_label(R.gate_report_sha256(report))
    cli.fail, cli.deny_merges = "merge:production", {"1:merge:previous"}
    record = write_json(tmp_path / "push.json", pushed("3"))
    assert R.main(["promote", "--push-record", str(record), "--gate-report",
                   str(write_json(tmp_path / "gate.json", report))]) == 2
    assert "Fix by hand" in capsys.readouterr().err


def test_rollback_restores_the_previous_version(settings):
    client = FakeRegistry(settings, versions=2)
    promote(settings, client, "1", gate())
    promote(settings, client, "2", gate("promote", champion="1"))
    record = R.rollback(settings, client)
    assert (record["from"], record["to"]) == ("2", "1")
    assert client.aliases() == {"1": ["previous", "production"]}  # ADR-006: both name it until the next promotion


def test_rollback_to_a_named_version(settings):
    client = FakeRegistry(settings, versions=3)
    promote(settings, client, "3", gate())
    assert R.rollback(settings, client, to="1")["to"] == "1"
    with pytest.raises(R.RegistryError, match="has no version '9'"):
        R.rollback(settings, client, to="9")


def test_rollback_needs_something_to_go_back_to(settings):
    with pytest.raises(R.RegistryError, match="no registry model"):
        R.rollback(settings, FakeRegistry(settings))
    client = FakeRegistry(settings, versions=1)
    promote(settings, client, "1", gate())
    with pytest.raises(R.RegistryError, match="nothing to roll back to"):
        R.rollback(settings, client)
    with pytest.raises(R.RegistryError, match="already holds production"):
        R.rollback(settings, client, to="1")


def test_status_lists_versions_newest_first(settings):
    assert R.status(settings, FakeRegistry(settings)) == []
    client = FakeRegistry(settings, versions=2)
    promote(settings, client, "2", gate())
    rows = R.status(settings, client)
    assert [(r["version"], r["aliases"]) for r in rows] == [("2", ["production"]), ("1", [])]


# --- re-score ----------------------------------------------------------------


def champion_in_bucket(settings, client, model_bytes=b"model", recorded=None):
    """A production version whose files are in the fake bucket."""
    folder = "surge-forecast/old-run"
    client.objects[f"{folder}/model.joblib"] = model_bytes
    client.objects[f"{folder}/run.json"] = json.dumps({"inputs": ["x"], "threshold": 0.5}).encode()
    digest = recorded or "sha256:" + hashlib.sha256(model_bytes).hexdigest()
    client.objects[f"{folder}/metadata.json"] = json.dumps({"data_version": "sha256:old",
                                                            "files": {"model.joblib": digest}}).encode()
    client.model = MODEL
    version = client._add({"versionDescription": f"run, files gs://{settings.model_bucket}/{folder}/ (metadata.json)"})
    client.merge_aliases(MODEL, version, add=("production",))
    return version


@pytest.fixture
def scored(monkeypatch):
    """joblib.load and score_champion replaced: the champion scores tp 9, fp 2, fn 3."""
    import joblib

    monkeypatch.setattr(joblib, "load", lambda path: "a model")
    monkeypatch.setattr(training_data, "data_version", lambda lock: DATA)
    monkeypatch.setattr(R, "score_champion", lambda saved, run, training: {
        "tp": 9, "fp": 2, "fn": 3, "threshold": 0.4, "rows": 100})


def test_rescore_with_no_champion_is_null(settings, training, monkeypatch):
    monkeypatch.setattr(training_data, "data_version", lambda lock: DATA)
    block, record = R.rescore(settings, training, FakeRegistry(settings))
    assert block is None and "no version holds production" in record["reason"]


def test_rescore_scores_the_production_version(settings, training, scored):
    client = FakeRegistry(settings)
    version = champion_in_bucket(settings, client)
    block, record = R.rescore(settings, training, client)
    assert block == {"tp": 9, "fp": 2, "fn": 3, "data_version": DATA, "model_version": version}
    assert record["trained_on"] == "sha256:old" and record["threshold"] == 0.4


def test_rescore_refuses_a_changed_model_file(settings, training, scored):
    client = FakeRegistry(settings)
    champion_in_bucket(settings, client, recorded="sha256:" + "0" * 64)
    with pytest.raises(R.RegistryError, match="does not match the sha256"):
        R.rescore(settings, training, client)


def test_rescore_refuses_a_model_that_does_not_load(settings, training, monkeypatch):
    import joblib

    def broken(path):
        raise ModuleNotFoundError("No module named 'xgboost'")

    monkeypatch.setattr(joblib, "load", broken)
    monkeypatch.setattr(training_data, "data_version", lambda lock: DATA)
    client = FakeRegistry(settings)
    champion_in_bucket(settings, client)
    with pytest.raises(R.RegistryError, match="does not load: ModuleNotFoundError"):
        R.rescore(settings, training, client)


def test_files_folder_must_be_the_model_bucket(settings):
    with pytest.raises(R.RegistryError, match="does not say where its files are"):
        R.files_folder(settings, {"versionId": "1", "versionDescription": "no files here"})
    with pytest.raises(R.RegistryError, match="not the model bucket"):
        R.files_folder(settings, {"versionId": "1", "versionDescription": "files gs://other/x/ (metadata.json)"})


def test_score_champion_uses_the_shared_threshold_rule(monkeypatch, training):
    import pandas as pd
    from sklearn.linear_model import LogisticRegression

    table = pd.DataFrame({"x": [0.0, 1, 2, 3, 4, 5, 6, 7], "label": [0, 0, 0, 1, 0, 1, 1, 1],
                          "split": ["validation"] * 7 + ["train"], "scored": [True] * 8})
    saved = LogisticRegression().fit(table[["x"]], table["label"])
    monkeypatch.setattr(training_data, "load_table", lambda t: (table, {}))
    scores = R.score_champion(saved, {"inputs": ["x"]}, training)
    assert scores["rows"] == 7 and scores["tp"] + scores["fn"] == 3
    renamed = type("Renamed", (), {"feature_names_in_": ["gone"]})()  # a champion trained on a dropped input
    with pytest.raises(R.RegistryError, match="does not have: \\['gone'\\]"):
        R.score_champion(renamed, {}, training)


def test_set_champion_block_compares_like_with_like():
    metrics = {"data_version": DATA, "candidate": {"tp": 9, "fp": 1, "fn": 3}}
    block = {"tp": 8, "fp": 2, "fn": 4, "data_version": DATA, "model_version": "1"}
    assert R.set_champion_block(metrics, block)["champion"] == block
    assert R.set_champion_block(metrics, None)["champion"] is None
    with pytest.raises(R.RegistryError, match="different validation rows"):
        R.set_champion_block(metrics, {**block, "fn": 5})
    with pytest.raises(R.RegistryError, match="re-scored on"):
        R.set_champion_block(metrics, {**block, "data_version": "sha256:other"})


# --- MLflow tag --------------------------------------------------------------


def test_mlflow_tag_failure_is_a_reason_not_an_error(training, monkeypatch):
    monkeypatch.undo()  # the real tag_mlflow
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")  # nothing listens there
    problem = R.tag_mlflow(training, MLFLOW_RUN, {"registry_version": "1"})
    assert problem and "ConnectionError" in problem


# --- command line ------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, settings, training):
    """main() with training.csv pointing at tmp_path and VertexClient replaced by one FakeRegistry."""
    client = FakeRegistry(settings)
    monkeypatch.setattr(training_data, "load_settings", lambda path=None: training)
    monkeypatch.setattr(V, "VertexClient", lambda s: client)
    return client


def test_cli_push_promote_status_rollback(cli, tmp_path, capsys):
    gate_file = write_json(tmp_path / "gate.json", gate())
    record = tmp_path / "push.json"
    assert R.main(["push", "--gate-report", str(gate_file), "--out", str(record)]) == 0
    assert R.main(["promote", "--push-record", str(record), "--gate-report", str(gate_file),
                   "--out", str(tmp_path / "promo.json")]) == 0
    assert json.loads((tmp_path / "promo.json").read_text())["decision"] == "promote"
    assert R.main(["status"]) == 0
    assert "version    1  candidate,production" in capsys.readouterr().out
    assert R.main(["rollback"]) == 2  # nothing holds previous after the first promotion
    assert "nothing to roll back to" in capsys.readouterr().err


def test_cli_push_dry_run_writes_nothing(cli, tmp_path, capsys):
    assert R.main(["push", "--gate-report", str(write_json(tmp_path / "g.json", gate())), "--dry-run"]) == 0
    assert cli.calls == [] and '"folder": "surge-forecast/' in capsys.readouterr().out


def test_cli_push_and_promote_skip_without_access(monkeypatch, training, tmp_path, capsys):
    def no_access(settings):
        raise V.NoAccess("no Google credentials")

    monkeypatch.setattr(training_data, "load_settings", lambda path=None: training)
    monkeypatch.setattr(V, "VertexClient", no_access)
    gate_file = write_json(tmp_path / "gate.json", gate())
    record = tmp_path / "push.json"
    assert R.main(["push", "--gate-report", str(gate_file), "--out", str(record)]) == 0
    assert "registry push skipped" in capsys.readouterr().out
    assert R.main(["promote", "--push-record", str(record), "--gate-report", str(gate_file)]) == 0
    assert "promotion skipped" in capsys.readouterr().out
    assert R.main(["status"]) == 2  # a person runs status and rollback: no access is an error


def test_cli_bad_input_exits_2(cli, tmp_path, capsys):
    assert R.main(["push", "--gate-report", str(write_json(tmp_path / "g.json", gate("blocked")))]) == 2
    assert "the gates decided 'blocked'" in capsys.readouterr().err
    assert R.main(["push", "--gate-report", str(tmp_path / "missing.json")]) == 2


def test_cli_rescore_sets_the_champion_in_place(cli, settings, tmp_path, scored, capsys):
    version = champion_in_bucket(settings, cli)
    metrics = write_json(tmp_path / "metrics.json", {"data_version": DATA, "candidate": {"tp": 10, "fp": 1, "fn": 2}})
    assert R.main(["rescore", "--metrics", str(metrics), "--out", str(tmp_path / "r.json")]) == 0
    assert json.loads(metrics.read_text())["champion"]["model_version"] == version
    assert "champion version" in capsys.readouterr().out


def test_cli_rescore_without_access_leaves_champion_null(monkeypatch, training, tmp_path, capsys):
    def no_access(settings):
        raise V.NoAccess("refused (403)")

    monkeypatch.setattr(training_data, "load_settings", lambda path=None: training)
    monkeypatch.setattr(V, "VertexClient", no_access)
    metrics = write_json(tmp_path / "metrics.json", {"data_version": DATA, "candidate": {"tp": 1, "fp": 0, "fn": 0}})
    assert R.main(["rescore", "--metrics", str(metrics)]) == 0
    assert json.loads(metrics.read_text())["champion"] is None
    assert "re-score skipped" in capsys.readouterr().out
