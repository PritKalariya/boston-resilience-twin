"""Vertex AI Model Registry push, champion and rollback (#111, ADR-006).

The Vertex AI Model Registry in project 2 is the only model registry (ADR-006). Every setting comes
from Model-Development/registry/registry.csv (load_settings), never from code: the project, region,
model name, model bucket, serving image, the three aliases and the
dvc.lock files whose code dependencies are checked against the tree.

Push (`python Model-Development/scripts/registry.py push --gate-report FILE`): the winner in
selection.json (training.csv reports_dir), after the gates passed, becomes a new version of the registry model with the alias
candidate (ADR-006 step 2). Its files go to a new folder of the model bucket that is never
overwritten, with metadata.json beside them: metrics, data version, git commit, MLflow run id,
training image digest, the train, validation and test windows, the gates.csv sha256 it passed and
the sha256 of every file. The version's labels and description repeat the short ones. The MLflow
run is then tagged with the registry version when MLflow can be reached; when it cannot, a log
line says so and the push still succeeds. Without project 2 access the push logs
`registry push skipped: ...` and exits 0, so a run without access still succeeds (#111).

Exit codes: 0 pushed or skipped, 2 the push cannot go ahead (a message names why).

Promote (`registry.py promote --push-record FILE --gate-report FILE`): the rollback rule. The pushed
version becomes champion (alias production, the old champion gets previous) only if gates.py
decided promote, which means it beat the champion's validation F1, re-scored on the same data
version, and passed the bias gate (ADR-026); otherwise the champion stays. Skips when the push did.

Roll back (`registry.py rollback [--to VERSION]`): production moves back to the version that holds
previous (or to VERSION). Status (`registry.py status`): every version with its aliases.

Re-score (`registry.py rescore --metrics FILE [--out FILE]`): before the gates run, the version that
holds production is downloaded from the model bucket (its model.joblib checked against the sha256 in
its metadata.json) and scored on the validation rows of the challenger's data version, with the same
shared threshold rule (training.csv threshold_rule). Its tp, fp and fn, the data version and its
version id go into the gates input FILE as `champion`, null when no version holds production. It
exits 2 when the champion needs an input the challenger's data does not have. Without project 2
access `champion` stays null and a log line says so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import training_data

# Model-Development/scripts/registry.py -> parents[2] is the repo root.
ROOT = Path(__file__).resolve().parents[2]
# Not in Model-Development/config: the training stage (Model-Development/dvc.yaml) depends on that whole
# folder, and a registry setting must not retrain the model.
DEFAULT_CONFIG = ROOT / "Model-Development" / "registry" / "registry.csv"
TEXT_SETTINGS = ("project", "region", "model_display_name", "model_bucket", "serving_image")
ALIAS_SETTINGS = ("alias_candidate", "alias_production", "alias_previous")
PATH_LIST_SETTINGS = ("dvc_locks",)
SETTING_NAMES = (*TEXT_SETTINGS, *ALIAS_SETTINGS, *PATH_LIST_SETTINGS)

# Vertex moves the default alias by itself when a version is deleted, so the serving version would
# change without a promotion or rollback (ADR-006: production and previous are custom aliases).
RESERVED_ALIASES = ("default",)
# Vertex aliases (model-alias docs and MergeVersionAliasesRequest): a lower case letter, then lower case
# letters, digits and hyphens, ending in a letter or digit, 2 to 128 characters.
ALIAS_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,126}[a-z0-9]")
REGION_PATTERN = re.compile(r"[a-z]+-[a-z]+[0-9]+")
PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]")


class RegistryError(ValueError):
    """A registry setting or registry step that cannot go ahead."""


@dataclass(frozen=True)
class Settings:
    project: str  # project 2 (ADR-009)
    region: str  # the registry's region; the model bucket and image repository share it (ADR-006)
    model_display_name: str  # the one registry model; every push is a new version of it
    model_bucket: str  # model files, a new path per version, never overwritten (ADR-006)
    serving_image: str  # the container every version names, without a tag
    alias_candidate: str
    alias_production: str  # the champion
    alias_previous: str  # what a rollback restores
    dvc_locks: tuple[Path, ...]  # step 6: code dependency md5 values checked against the tree

    @property
    def aliases(self) -> dict[str, str]:
        return {"candidate": self.alias_candidate, "production": self.alias_production,
                "previous": self.alias_previous}


def _path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_settings(path: str | Path = DEFAULT_CONFIG, root: str | Path = ROOT) -> Settings:
    """Load and check registry.csv. Relative paths are read from the repo root (root).

    Fails, naming the setting, on a missing, unknown, duplicate or empty setting, a project, region
    or bucket that is not a valid name, a serving image with a tag or digest, an alias that is not a
    valid Vertex alias or is reserved (default), or two aliases with the same value.
    """
    path, root = Path(path), Path(root)
    try:
        pairs = training_data._read_pairs(path)
    except training_data.SettingsError as exc:
        raise RegistryError(str(exc)) from None
    unknown = sorted(set(pairs) - set(SETTING_NAMES))
    if unknown:
        raise RegistryError(f"unknown setting(s) in {path.name}: {unknown}")
    missing = [name for name in SETTING_NAMES if name not in pairs]
    if missing:
        raise RegistryError(f"missing setting(s) in {path.name}: {missing}")
    empty = [name for name in SETTING_NAMES if not pairs[name]]
    if empty:
        raise RegistryError(f"setting(s) with no value in {path.name}: {empty}")

    for name, pattern in (("project", PROJECT_PATTERN), ("region", REGION_PATTERN),
                          ("model_bucket", BUCKET_PATTERN)):
        if not pattern.fullmatch(pairs[name]):
            raise RegistryError(f"{name} is not a valid name: {pairs[name]!r}")
    image = pairs["serving_image"]
    if "@" in image or ":" in image.rsplit("/", 1)[-1]:
        raise RegistryError(f"serving_image must have no tag or digest (the push adds the digest): {image!r}")

    for name in ALIAS_SETTINGS:
        alias = pairs[name]
        if alias in RESERVED_ALIASES:
            raise RegistryError(f"{name} cannot be {alias!r}: Vertex moves it by itself (ADR-006)")
        if not ALIAS_PATTERN.fullmatch(alias):
            raise RegistryError(f"{name} is not a valid alias (lower case letters, digits, hyphens): {alias!r}")
    values = [pairs[name] for name in ALIAS_SETTINGS]
    if len(set(values)) != len(values):
        raise RegistryError(f"the aliases must differ: {dict(zip(ALIAS_SETTINGS, values))}")

    locks = tuple(_path(part.strip(), root) for part in pairs["dvc_locks"].split(";") if part.strip())
    if not locks:
        raise RegistryError("dvc_locks needs at least one path")

    return Settings(
        **{name: pairs[name] for name in (*TEXT_SETTINGS, *ALIAS_SETTINGS)},
        dvc_locks=locks,
    )


# ---- push (ADR-006 step 2)

PUSH_DECISIONS = ("promote", "keep_champion")  # gates.py decisions that let a model be registered
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
LABEL_MAX = 63  # Vertex label values: lower case letters, digits, _ and -, at most 63 characters
# A sha256 (64 hex) does not fit a label, so labels keep its first HASH_LABEL hex characters, like a
# short git hash; metadata.json and the version description keep the full value.
HASH_LABEL = 32
RUN_FILES = ("model.joblib", "run.json", "metrics.json", "validation_counts.json")


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"cannot read the {what} {path}: {exc}") from None
    except ValueError as exc:
        raise RegistryError(f"the {what} {path} is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise RegistryError(f"the {what} {path} is not a JSON object")
    return data


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _label(value: object) -> str:
    """A Vertex label value: lower case, only [a-z0-9_-]; fails rather than cut a value short."""
    text = re.sub(r"[^a-z0-9_-]", "-", str(value).lower())
    if len(text) > LABEL_MAX:
        raise RegistryError(f"label value longer than {LABEL_MAX} characters: {text!r}")
    return text


def _hash_label(value: str) -> str:
    """The first HASH_LABEL hex characters of a sha256, with or without its sha256: prefix."""
    return value.removeprefix("sha256:")[:HASH_LABEL]


def gate_report_sha256(gate_report: dict) -> str:
    """The sha256 of a gate report's content (sorted keys), the file the push uploads beside the model."""
    return "sha256:" + hashlib.sha256(json.dumps(gate_report, sort_keys=True).encode("utf-8")).hexdigest()


@contextmanager
def _access_checked():
    """After the first call to project 2 answered, a refusal is a missing role or expired credentials:
    an error (exit 2), never a skip, so a run cannot go green with the registry half changed."""
    import vertex_client

    try:
        yield
    except vertex_client.NoAccess as exc:
        raise RegistryError(f"project 2 refused a call after the access check: {exc}") from None


def _winner(selection: dict) -> dict:
    name = selection.get("winner")
    if not name:
        raise RegistryError(f"selection.json has no winner: {selection.get('decision', 'no decision')}")
    rows = [row for row in selection.get("ranking", []) if row.get("model") == name]
    if len(rows) != 1:
        raise RegistryError(f"selection.json names winner {name!r} but its ranking has {len(rows)} rows for it")
    return rows[0]


DIRTY = "the run was trained on a tree with uncommitted changes; commit them and train again"


def _commit(run: dict, allow_dirty: bool = False) -> str:
    """The run's git commit: from run.json, else GITHUB_SHA (the training image has no .git). Fails
    on a tree with uncommitted changes, unless allow_dirty (a dry run): a registered version must
    trace to one exact commit."""
    git = run.get("git") or {}
    if git.get("uncommitted_changes"):
        if not allow_dirty:
            raise RegistryError(DIRTY)
        print(f"warning: {DIRTY} (a real push stops here)", file=sys.stderr)
    commit = git.get("commit") or os.environ.get("GITHUB_SHA", "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise RegistryError(f"the run has no git commit (run.json git.commit or GITHUB_SHA): {commit!r}")
    return commit


def winner_from_evaluation(evaluation_path: Path, training: training_data.Settings) -> tuple[dict, dict]:
    """The model workflow's winner (#112, ADR-032): (a selection.json-shaped report, its winner's row) from
    the evaluate step's evaluation.json and the run's metrics.json, validation block only (no step reads a
    test metric). The report's data version is the run's; build_push checks it against the gate report."""
    evaluation = _read_json(evaluation_path, "evaluation report")
    name, folder = evaluation.get("winner"), evaluation.get("winner_run")
    if not name or not folder:
        raise RegistryError(f"{evaluation_path} names no winner and winner_run: {evaluation.get('selected')!r}")
    run_dir = Path(training.runs_dir) / str(folder)
    run = _read_json(run_dir / "run.json", "run record")
    validation = _read_json(run_dir / "metrics.json", "run metrics").get("validation")
    row = {"model": name, "run_folder": str(folder), "mlflow_run_id": (run.get("mlflow") or {}).get("run_id"),
           "validation": validation, "test": None}
    report = {"winner": name, "ranking": [row], "data_version": run.get("data_version"),
              "selection_id": None, "decision": evaluation.get("selected")}
    return report, row


def build_push(settings: Settings, training: training_data.Settings, gate_report: dict,
               image_digest: str | None = None, allow_dirty: bool = False,
               evaluation_path: Path | None = None) -> dict:
    """Everything a push sends, read and checked before any call to project 2: the files to upload,
    their sha256 values and the version's metadata. Fails naming what is wrong.

    The winner comes from selection.json (training.csv reports_dir), or, with evaluation_path, from
    the model workflow's evaluation.json (the evaluate step picks it after the bias check)."""
    decision = gate_report.get("decision")
    if decision not in PUSH_DECISIONS:
        raise RegistryError(f"the gates decided {decision!r}; only {' or '.join(PUSH_DECISIONS)} is registered")
    gates_sha256 = gate_report.get("gates_sha256")
    if not isinstance(gates_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", gates_sha256):
        raise RegistryError(f"the gate report has no gates_sha256: {gates_sha256!r}")
    if image_digest is not None and not DIGEST_PATTERN.fullmatch(image_digest):
        raise RegistryError(f"--image-digest must be sha256:<64 hex>, got {image_digest!r}")

    if evaluation_path is None:
        selection_path = Path(training.reports_dir) / "selection.json"
        selection = _read_json(selection_path, "selection report")
        row = _winner(selection)
    else:
        selection_path = Path(evaluation_path)
        selection, row = winner_from_evaluation(selection_path, training)
    run_dir = Path(training.runs_dir) / row["run_folder"]
    run = _read_json(run_dir / "run.json", "run record")
    missing = [name for name in RUN_FILES if not (run_dir / name).is_file()]
    if missing:
        raise RegistryError(f"the winner's run folder {run_dir} is missing {missing}")

    versions = {selection_path.name: selection.get("data_version"), "run.json": run.get("data_version"),
                "gate report": gate_report.get("data_version")}
    if len(set(versions.values())) != 1 or not versions["run.json"]:
        raise RegistryError(f"the data versions differ: {versions}")
    mlflow_run_id = row.get("mlflow_run_id") or (run.get("mlflow") or {}).get("run_id")
    if not mlflow_run_id:
        raise RegistryError("the winner has no MLflow run id")
    splits = (run.get("settings") or {}).get("splits")
    if not isinstance(splits, dict) or set(splits) != set(training_data.SPLITS):
        raise RegistryError(f"run.json settings.splits must name {list(training_data.SPLITS)}: {splits!r}")

    commit = _commit(run, allow_dirty)
    if not re.fullmatch(r"[0-9a-zA-Z_-]+", str(mlflow_run_id)):
        raise RegistryError(f"the MLflow run id is not a plain id: {mlflow_run_id!r}")
    # The MLflow run id makes the folder unique per training run: the model workflow reuses the run ids
    # ci-<model> on every run, and the bucket never overwrites a file.
    folder = f"{settings.model_display_name}/{run['run_id']}/{mlflow_run_id}"
    files = {name: run_dir / name for name in RUN_FILES}
    files[selection_path.name] = selection_path  # selection.json, or the workflow's evaluation.json
    metadata = {
        "model": row["model"], "run_id": run["run_id"], "selection_id": selection.get("selection_id"),
        "data_version": versions["run.json"], "git_commit": commit, "mlflow_run_id": mlflow_run_id,
        "mlflow_experiment": (run.get("mlflow") or {}).get("experiment"),
        "training_image_digest": image_digest,
        "windows": {split: {"start": first, "end": last} for split, (first, last) in splits.items()},
        "threshold": run.get("threshold"),
        "validation": row.get("validation"), "test": row.get("test"),
        "gate": {"decision": decision, "reason": gate_report.get("reason"), "gates_sha256": gates_sha256,
                 "fairness_group_sha256": gate_report.get("fairness_group_sha256"),
                 "report_sha256": gate_report_sha256(gate_report)},
        "files": {name: _sha256(path) for name, path in files.items()},
        "pushed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    validation = row.get("validation") or {}
    labels = {"model": row["model"], "data_version": _hash_label(versions["run.json"]), "git_commit": commit,
              "mlflow_run_id": mlflow_run_id, "gate_decision": decision, "gates_sha256": _hash_label(gates_sha256),
              "gate_report": _hash_label(gate_report_sha256(gate_report))}
    if image_digest:
        labels["image_digest"] = _hash_label(image_digest)
    files_uri = f"gs://{settings.model_bucket}/{folder}/"
    image = f"{settings.serving_image}@{image_digest}" if image_digest else settings.serving_image
    description = (f"{row['model']} run {run['run_id']}, validation F1 {validation.get('f1')}, "
                   f"data {versions['run.json']}, commit {commit}, MLflow run {mlflow_run_id}, "
                   f"gates {decision} (gates.csv {gates_sha256}), files {files_uri} (metadata.json)")
    # No artifactUri: it is optional for a custom container, and Vertex would copy the folder into its own
    # storage at upload, which needs its service agent to read the bucket. Nothing is served from Vertex
    # (ADR-006), so the description and the push record carry where the files are instead.
    return {"folder": folder, "files": files, "metadata": metadata, "files_uri": files_uri,
            "model": {"displayName": settings.model_display_name,
                      "containerSpec": {"imageUri": image},
                      "labels": {key: _label(value) for key, value in labels.items()},
                      "versionDescription": description}}


def tag_mlflow(training: training_data.Settings, mlflow_run_id: str, tags: dict[str, str]) -> str | None:
    """Tag the MLflow run with the registry version; a reason string when MLflow cannot be reached
    (the push still succeeds), None when tagged."""
    import mlflow_runs
    import tracking

    uri = mlflow_runs.tracking_uri(training)
    try:
        if os.environ.get(tracking.ENV, "").strip():
            tracking.check_server(uri)
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=uri)
        for key, value in tags.items():
            client.set_tag(mlflow_run_id, key, value)
    except Exception as exc:  # any MLflow failure: the registry version is the record, the tag a pointer
        return f"{type(exc).__name__}: {exc}"
    return None


def push(settings: Settings, training: training_data.Settings, client, gate_report: dict,
         image_digest: str | None = None, evaluation_path: Path | None = None) -> dict:
    """Register the winner as a new version with the candidate alias; returns the push record."""
    plan = build_push(settings, training, gate_report, image_digest, evaluation_path=evaluation_path)
    parent = client.find_model()  # the access check: a NoAccess here means skip, before anything is written
    with _access_checked():
        return _push(settings, training, client, plan, gate_report, parent)


def _push(settings: Settings, training: training_data.Settings, client, plan: dict, gate_report: dict,
          parent: str | None) -> dict:
    folder, metadata = plan["folder"], plan["metadata"]
    with tempfile.TemporaryDirectory() as scratch:
        extra = {"gate_report.json": gate_report, "metadata.json": metadata}
        uploads = dict(plan["files"])
        for name, data in extra.items():
            path = Path(scratch) / name
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            uploads[name] = path
        uris = {name: client.upload_file(path, f"{folder}/{name}") for name, path in uploads.items()}
    model_name, version_id = client.upload_version(plan["model"], parent)
    client.merge_aliases(model_name, version_id, add=(settings.alias_candidate,))
    record = {"model_name": model_name, "version_id": version_id, "version": f"{model_name}@{version_id}",
              "alias": settings.alias_candidate, "files_uri": plan["files_uri"],
              "files": uris, "metadata": metadata}
    problem = tag_mlflow(training, metadata["mlflow_run_id"],
                         {"registry_model": model_name, "registry_version": version_id})
    record["mlflow_tagged"] = problem is None
    if problem:
        print(f"mlflow tag skipped: {problem}", file=sys.stderr)
    return record


# ---- promote, roll back, status (ADR-006 steps 4 and 5)


def _id(ref: object) -> str:
    """A version id from an id, a version record or a name@id reference."""
    if isinstance(ref, dict):
        ref = ref.get("versionId")
    return str(ref).rsplit("@", 1)[-1]


def _holder(versions: list[dict], alias: str) -> dict | None:
    return next((v for v in versions if alias in v.get("versionAliases", [])), None)


def decide(gate_report: dict, data_version: str, production: dict | None) -> bool:
    """The rollback rule: True when the challenger becomes champion, False when the champion stays.

    gates.py has already compared the challenger with the champion's validation F1 and applied the
    bias gate (ADR-026), so its decision is promote or keep_champion. This checks it compared with the
    right champion: the version that holds production now, re-scored on the challenger's data version
    (#111). With no production yet (the first version), the report must have no champion block."""
    decision = gate_report.get("decision")
    if gate_report.get("data_version") != data_version:
        raise RegistryError(f"the gate report's data version {gate_report.get('data_version')!r} is not the "
                            f"pushed version's {data_version!r}")
    champion = gate_report.get("champion")
    if production is None:
        if champion is not None:
            raise RegistryError("the gate report compares with a champion, but no version holds production")
    else:
        if champion is None:
            raise RegistryError(f"version {_id(production)} holds production but the gate report has no champion "
                                "block: re-score the champion on this data version first")
        if _id(champion.get("model_version")) != _id(production):
            raise RegistryError(f"the gate report's champion is version {champion.get('model_version')!r}, but "
                                f"version {_id(production)} holds production")
        if champion.get("data_version") != data_version:
            raise RegistryError("the champion was not re-scored on the challenger's data version")
    if decision == "promote":
        return True
    if decision == "keep_champion":
        return False
    raise RegistryError(f"the gates decided {decision!r}; only promote or keep_champion can be acted on")


def promote(settings: Settings, client, push_record: dict, gate_report: dict) -> dict:
    """Make the pushed version champion if the rollback rule allows, else keep the champion.

    Works on the pushed version id, never on whatever holds candidate by then. Captures the version
    that holds production, moves previous to it, then production to the challenger; if that second
    move fails, previous goes back where it was (ADR-006 step 4). Cloud Run traffic is not moved
    here: the forecast API does not exist yet."""
    model_name, version_id = push_record["model_name"], _id(push_record["version_id"])
    pushed = client.get_version(model_name, version_id)  # the access check: a NoAccess here means skip
    with _access_checked():
        return _promote(settings, client, push_record, gate_report, pushed)


def _promote(settings: Settings, client, push_record: dict, gate_report: dict, pushed: dict) -> dict:
    model_name, version_id = push_record["model_name"], _id(push_record["version_id"])
    data_version = push_record["metadata"]["data_version"]
    labels = pushed.get("labels", {})
    expected = {"data_version": _hash_label(data_version),
                "gates_sha256": _hash_label(str(gate_report.get("gates_sha256", ""))),
                "gate_report": _hash_label(gate_report_sha256(gate_report))}
    wrong = {key: labels.get(key) for key, value in expected.items() if labels.get(key) != value}
    if wrong:
        raise RegistryError(f"version {version_id} does not carry this gate report's values: {wrong}")

    versions = client.list_versions(model_name)
    production = _holder(versions, settings.alias_production)
    previous = _holder(versions, settings.alias_previous)
    if production is not None and _id(production) == version_id:
        raise RegistryError(f"version {version_id} already holds {settings.alias_production}")
    record = {"model_name": model_name, "challenger": version_id,
              "champion_before": None if production is None else _id(production),
              "previous_before": None if previous is None else _id(previous),
              "decided_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not decide(gate_report, data_version, production):
        return {**record, "decision": "keep_champion", "champion": record["champion_before"]}

    captured = record["champion_before"]
    if captured is not None:
        client.merge_aliases(model_name, captured, add=(settings.alias_previous,))
    production_alias, previous_alias = settings.alias_production, settings.alias_previous
    try:
        client.merge_aliases(model_name, version_id, add=(production_alias,))
    except Exception as exc:
        if captured is None:  # nothing was moved before
            raise RegistryError(f"moving {production_alias} to version {version_id} failed: {exc}") from None
        before = record["previous_before"]
        try:  # put previous back as it was, then report the failure
            if before is not None:
                client.merge_aliases(model_name, before, add=(previous_alias,))
            else:
                client.merge_aliases(model_name, captured, remove=(previous_alias,))
        except Exception as again:  # never lose the first error, never exit 0 with previous moved
            should = f"version {before}" if before is not None else "no version"
            raise RegistryError(
                f"moving {production_alias} to version {version_id} failed ({exc}), and putting "
                f"{previous_alias} back also failed ({again}). Fix by hand (runbook \"Model registry\"): "
                f"{previous_alias} is on version {captured} and should be on {should}; {production_alias} "
                f"is still on version {captured}") from None
        raise RegistryError(f"moving {production_alias} to version {version_id} failed; "
                            f"{previous_alias} was put back: {exc}") from None
    return {**record, "decision": "promote", "champion": version_id, "previous": captured}


def rollback(settings: Settings, client, to: str | None = None) -> dict:
    """Set the champion back: production moves to the version that holds previous, or to `to`.

    After a rollback production and previous name the same version until the next promotion, and the
    version that was rolled back from keeps only its other aliases (ADR-006 step 5)."""
    model_name = client.find_model()
    if model_name is None:
        raise RegistryError(f"no registry model named {settings.model_display_name!r}")
    versions = client.list_versions(model_name)
    production = _holder(versions, settings.alias_production)
    if to is None:
        target = _holder(versions, settings.alias_previous)
        if target is None:
            raise RegistryError(f"no version holds {settings.alias_previous}: nothing to roll back to")
    else:
        target = next((v for v in versions if _id(v) == _id(to)), None)
        if target is None:
            raise RegistryError(f"model {model_name} has no version {to!r}")
    if production is not None and _id(production) == _id(target):
        raise RegistryError(f"version {_id(target)} already holds {settings.alias_production}")
    client.merge_aliases(model_name, _id(target), add=(settings.alias_production,))
    return {"model_name": model_name, "from": None if production is None else _id(production),
            "to": _id(target), "rolled_back_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def status(settings: Settings, client) -> list[dict]:
    """Every version with its aliases and the labels that trace it, newest first."""
    model_name = client.find_model()
    if model_name is None:
        return []
    rows = []
    for version in client.list_versions(model_name):
        labels = version.get("labels", {})
        rows.append({"version": _id(version), "aliases": sorted(version.get("versionAliases", [])),
                     "created": version.get("versionCreateTime", ""), "model": labels.get("model", ""),
                     "data_version": labels.get("data_version", ""), "git_commit": labels.get("git_commit", "")[:12],
                     "gate_decision": labels.get("gate_decision", ""), "model_name": model_name})
    return sorted(rows, key=lambda row: int(row["version"]) if row["version"].isdigit() else 0, reverse=True)


# ---- re-score the champion (#111: on the challenger's data version, before the comparison)

FILES_PATTERN = re.compile(r"files gs://([^/\s]+)/(\S+?)/ \(metadata\.json\)")


def files_folder(settings: Settings, version: dict) -> str:
    """The bucket folder a version's files are in, from its description (the push writes it there)."""
    found = FILES_PATTERN.search(version.get("versionDescription", "") or version.get("description", ""))
    if found is None:
        raise RegistryError(f"version {_id(version)} does not say where its files are")
    if found.group(1) != settings.model_bucket:
        raise RegistryError(f"version {_id(version)} files are in gs://{found.group(1)}, not the model bucket")
    return found.group(2)


def score_champion(saved, run: dict, training: training_data.Settings) -> dict:
    """tp, fp and fn of a saved model on the scored validation rows of the current data, at the
    threshold the shared rule picks on them. Fails naming any input the data does not have."""
    import numpy as np

    import threshold

    table, _ = training_data.load_table(training)
    rows = table[(table["split"] == "validation") & table["scored"]].reset_index(drop=True)
    inputs = list(getattr(saved, "feature_names_in_", None) if getattr(saved, "feature_names_in_", None) is not None
                  else run.get("inputs") or [])
    if not inputs:
        raise RegistryError("the champion names no inputs (feature_names_in_ or run.json inputs)")
    missing = [column for column in inputs if column not in rows.columns]
    if missing:
        raise RegistryError(f"the champion needs inputs the challenger's data does not have: {missing}")
    probability = np.asarray(saved.predict_proba(rows[inputs]))[:, 1]
    label = rows["label"].to_numpy()
    cut = threshold.shared_threshold(probability, label, training.threshold_rule)
    counts = threshold.counts(probability, label, cut)
    return {"tp": counts.tp, "fp": counts.fp, "fn": counts.fn, "threshold": cut, "rows": len(rows)}


def rescore(settings: Settings, training: training_data.Settings, client) -> tuple[dict | None, dict]:
    """The champion block for the gates input, None with no champion, and a record of what was done."""
    data_version = training_data.data_version(training.dvc_lock)
    model_name = client.find_model()  # the access check: a NoAccess here means skip (champion null)
    with _access_checked():
        return _rescore(settings, training, client, data_version, model_name)


def _rescore(settings: Settings, training: training_data.Settings, client, data_version: str,
             model_name: str | None) -> tuple[dict | None, dict]:
    import joblib

    production = None if model_name is None else client.find_alias(model_name, settings.alias_production)
    if production is None:
        return None, {"champion": None, "reason": f"no version holds {settings.alias_production}",
                      "data_version": data_version}
    version_id = _id(production)
    folder = files_folder(settings, production)
    with tempfile.TemporaryDirectory() as scratch:
        local = {name: client.download_file(f"{folder}/{name}", Path(scratch) / name)
                 for name in ("metadata.json", "run.json", "model.joblib")}
        metadata = _read_json(local["metadata.json"], "champion metadata")
        expected = (metadata.get("files") or {}).get("model.joblib")
        if _sha256(local["model.joblib"]) != expected:
            raise RegistryError(f"version {version_id} model.joblib does not match the sha256 in its metadata.json")
        run = _read_json(local["run.json"], "champion run record")
        try:
            saved = joblib.load(local["model.joblib"])  # our own bucket, checked against its recorded sha256
        except Exception as exc:  # a missing library or a changed class: the champion cannot be scored
            raise RegistryError(f"version {version_id} model.joblib does not load: {type(exc).__name__}: {exc}") from None
    try:
        scores = score_champion(saved, run, training)
    except (ValueError, KeyError, AttributeError) as exc:  # the table or the model's scoring failed
        raise RegistryError(f"version {version_id} cannot be scored: {type(exc).__name__}: {exc}") from None
    block = {"tp": scores["tp"], "fp": scores["fp"], "fn": scores["fn"], "data_version": data_version,
             "model_version": version_id}
    record = {"champion": block, "model_name": model_name, "files": f"gs://{settings.model_bucket}/{folder}/",
              "trained_on": metadata.get("data_version"), "threshold": scores["threshold"],
              "threshold_at_training": run.get("threshold"), "rows": scores["rows"],
              "rescored_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return block, record


def set_champion_block(metrics: dict, block: dict | None) -> dict:
    """The gates input with `champion` set; fails when it would not compare like with like."""
    if block is not None:
        if block["data_version"] != metrics.get("data_version"):
            raise RegistryError(f"the gates input is for data version {metrics.get('data_version')!r}, the "
                                f"champion was re-scored on {block['data_version']!r}")
        candidate = metrics.get("candidate") or {}
        if block["tp"] + block["fn"] != candidate.get("tp", 0) + candidate.get("fn", 0):
            raise RegistryError("the champion and the candidate were scored on different validation rows "
                                f"(tp + fn {block['tp'] + block['fn']} and "
                                f"{candidate.get('tp', 0) + candidate.get('fn', 0)})")
    return {**metrics, "champion": block}


# ---- command line


def _write(record: dict, out: str | None) -> str:
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="registry.csv")
    parser.add_argument("--training-config", default=training_data.DEFAULT_CONFIG, help="training.csv")
    commands = parser.add_subparsers(dest="command", required=True)
    require = {"action": "store_true",
               "help": "no project 2 access is an error (exit 2), not a skip: the model workflow on main"}
    cmd = commands.add_parser("push", help="register selection.json's winner as the candidate")
    cmd.add_argument("--gate-report", required=True, help="gates.py --out file for this model")
    cmd.add_argument("--evaluation", help="the model workflow's evaluation.json: its winner instead of selection.json's")
    cmd.add_argument("--require-access", **require)
    cmd.add_argument("--image-digest", help="the training image digest, sha256:<64 hex>")
    cmd.add_argument("--out", help="write the push record JSON here (also when skipped)")
    cmd.add_argument("--dry-run", action="store_true", help="check the inputs and print the plan; no calls")
    cmd = commands.add_parser("promote", help="champion or keep: the rollback rule on a pushed version")
    cmd.add_argument("--push-record", required=True, help="push --out file")
    cmd.add_argument("--gate-report", required=True, help="the gates.py --out file the push used")
    cmd.add_argument("--out", help="write the promotion record JSON here (also when skipped)")
    cmd.add_argument("--require-access", **require)
    cmd = commands.add_parser("rollback", help="set the champion back to the previous version")
    cmd.add_argument("--to", help="a version id to roll back to instead of the one holding previous")
    cmd.add_argument("--out", help="write the rollback record JSON here")
    commands.add_parser("status", help="list the versions and their aliases")
    cmd = commands.add_parser("rescore", help="score the champion on this data version into the gates input")
    cmd.add_argument("--metrics", required=True, help="the gates input; its champion is set in place")
    cmd.add_argument("--out", help="write the re-score record JSON here")
    cmd.add_argument("--require-access", **require)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    import vertex_client

    try:
        settings = load_settings(args.config)
        if args.command == "push":
            return _cmd_push(args, settings, vertex_client)
        if args.command == "promote":
            return _cmd_promote(args, settings, vertex_client)
        if args.command == "rescore":
            return _cmd_rescore(args, settings, vertex_client)
        client = vertex_client.VertexClient(settings)  # rollback and status are run by a person: no skip
        if args.command == "rollback":
            record = rollback(settings, client, args.to)
            _write(record, args.out)
            print(f"rolled back: {settings.alias_production} moved from version {record['from']} to {record['to']}")
            return 0
        rows = status(settings, client)
        if not rows:
            print(f"no registry model named {settings.model_display_name!r} yet")
        for row in rows:
            print(f"version {row['version']:>4}  {','.join(row['aliases']) or '-':<28} {row['model']:<9} "
                  f"data {row['data_version'][:12]:<12} commit {row['git_commit']:<12} gates {row['gate_decision']:<13} "
                  f"{row['created']}")
        return 0
    except (RegistryError, training_data.SettingsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _cmd_push(args, settings: Settings, vertex_client) -> int:
    training = training_data.load_settings(args.training_config)
    gate_report = _read_json(Path(args.gate_report), "gate report")
    # bad inputs fail even without access; a dry run only warns about a dirty tree
    evaluation = Path(args.evaluation) if args.evaluation else None
    plan = build_push(settings, training, gate_report, args.image_digest, allow_dirty=args.dry_run,
                      evaluation_path=evaluation)
    if args.dry_run:
        plan["files"] = {name: str(path) for name, path in plan["files"].items()}
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    try:
        client = vertex_client.VertexClient(settings)
        record = push(settings, training, client, gate_report, args.image_digest, evaluation_path=evaluation)
    except vertex_client.NoAccess as exc:
        if args.require_access:
            raise RegistryError(f"registry push needs project 2 access: {exc}") from None
        _write({"skipped": f"no project 2 access ({exc})"}, args.out)
        print(f"registry push skipped: no project 2 access ({exc})")
        return 0
    text = _write(record, args.out)
    print(f"registered {record['version']} as {record['alias']} ({record['files_uri']})")
    print(text, end="")
    return 0


def _cmd_promote(args, settings: Settings, vertex_client) -> int:
    push_record = _read_json(Path(args.push_record), "push record")
    if "skipped" in push_record and args.require_access:
        raise RegistryError(f"the push was skipped, but this run needs project 2 access: {push_record['skipped']}")
    if "skipped" in push_record:
        _write({"skipped": push_record["skipped"]}, args.out)
        print(f"promotion skipped: the push was skipped ({push_record['skipped']})")
        return 0
    gate_report = _read_json(Path(args.gate_report), "gate report")
    try:
        client = vertex_client.VertexClient(settings)
        record = promote(settings, client, push_record, gate_report)
    except vertex_client.NoAccess as exc:
        if args.require_access:
            raise RegistryError(f"promotion needs project 2 access: {exc}") from None
        _write({"skipped": f"no project 2 access ({exc})"}, args.out)
        print(f"promotion skipped: no project 2 access ({exc})")
        return 0
    text = _write(record, args.out)
    if record["decision"] == "promote":
        print(f"promoted version {record['champion']} to {settings.alias_production} "
              f"(previous: {record['previous']})")
    else:
        print(f"kept the champion, version {record['champion']}; version {record['challenger']} stays a challenger")
    print(text, end="")
    return 0


def _cmd_rescore(args, settings: Settings, vertex_client) -> int:
    training = training_data.load_settings(args.training_config)
    path = Path(args.metrics)
    metrics = _read_json(path, "gates input")
    try:
        client = vertex_client.VertexClient(settings)
        block, record = rescore(settings, training, client)
    except vertex_client.NoAccess as exc:
        if args.require_access:
            raise RegistryError(f"the champion re-score needs project 2 access: {exc}") from None
        block, record = None, {"champion": None, "skipped": f"no project 2 access ({exc})"}
        print(f"champion re-score skipped: no project 2 access ({exc}); champion stays null")
    path.write_text(json.dumps(set_champion_block(metrics, block), indent=2) + "\n", encoding="utf-8")
    _write(record, args.out)
    if block is None:
        print(f"no champion: {record.get('reason') or record.get('skipped')}")
    else:
        print(f"champion version {block['model_version']} on {block['data_version'][:19]}: "
              f"tp {block['tp']} fp {block['fp']} fn {block['fn']} (threshold {record['threshold']:.4f})")
    return 0


if __name__ == "__main__":
    # Run through the module named registry, the one vertex_client imports, so both share RegistryError.
    import registry

    sys.exit(registry.main())
