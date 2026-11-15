"""Checks on .github/workflows/model.yml and the script it calls (#112, ADR-032).

The compose test service mounts .github read-only, so these run in CI's required job too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "model.yml"
SCRIPT = ROOT / ".github" / "scripts" / "in_image.sh"

READER = "ci-reader@boston-resilience-twin-p2.iam.gserviceaccount.com"
WRITER = "ci-writer@boston-resilience-twin-p2.iam.gserviceaccount.com"
PROVIDER = "projects/570429679915/locations/global/workloadIdentityPools/github/providers/github-oidc"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def steps(workflow) -> list[dict]:
    return workflow["jobs"]["pipeline"]["steps"]


def step(steps, name_start: str) -> dict:
    (found,) = [s for s in steps if s.get("name", "").startswith(name_start)]
    return found


def test_stub_outcome_is_gone():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "STUB_OUTCOME" not in text and "stub_outcome" not in text.replace("stubs", "")
    assert yaml.safe_load(text)[True]["workflow_dispatch"] is None  # no inputs


def test_permissions_are_read_only_except_the_token_of_the_pipeline_job(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["pipeline"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert "permissions" not in workflow["jobs"]["changes"] and "permissions" not in workflow["jobs"]["model-ci"]


def test_every_action_is_pinned_to_a_full_commit_sha():
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text(encoding="utf-8"))
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert any(u.startswith("google-github-actions/auth@") for u in uses)


def test_the_identities_and_the_provider_are_the_ones_of_project_2(steps):
    reader, writer = step(steps, "Authenticate to Google Cloud (ci-reader)"), step(steps, "Authenticate to Google Cloud (ci-writer)")
    assert reader["with"]["service_account"] == READER and writer["with"]["service_account"] == WRITER
    assert reader["with"]["workload_identity_provider"] == writer["with"]["workload_identity_provider"] == PROVIDER
    assert reader["if"] == "env.MAIN_RUN != 'true'" and writer["if"] == "env.MAIN_RUN == 'true'"
    assert writer["with"]["token_format"] == "access_token" and "token_format" not in reader["with"]
    assert "#131" in WORKFLOW.read_text(encoding="utf-8") and "ADR-030" in WORKFLOW.read_text(encoding="utf-8")


def test_the_pull_names_its_targets_and_never_pulls_everything(steps):
    run = step(steps, "Pull the training data")["run"]
    pulls = [line for line in run.splitlines() if line.strip().startswith("dvc pull")]
    assert pulls == ["dvc pull Data-Pipeline/data/processed/labels.parquet Data-Pipeline/data/processed/features.parquet"]


def test_the_image_push_is_main_only_right_after_the_build(steps):
    names = [s.get("name", "") for s in steps]
    assert names.index("Push the image (main only)") == names.index("Build the training image") + 1
    push = step(steps, "Push the image")
    assert push["if"] == "env.MAIN_RUN == 'true'"
    assert "--password-stdin" in push["run"] and "us-east1-docker.pkg.dev/boston-resilience-twin-p2/brt-images/brt-train" in (
        WORKFLOW.read_text(encoding="utf-8"))
    assert "artifacts/image_digest.txt" in push["run"] and "RepoDigests" in push["run"]


def test_the_order_and_the_main_only_steps(steps):
    names = [s.get("name", "") for s in steps]
    order = ["Check the data", "Train the baseline", "Evaluate", "Re-score the champion (main only)", "Gates", "Reload check",
             "Registry push (main only)", "Set champion (main only)"]
    positions = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
    assert positions == sorted(positions)
    for name in ("Re-score the champion", "Registry push", "Set champion"):
        assert step(steps, name)["if"] == "env.MAIN_RUN == 'true'"
    assert "if" not in step(steps, "Reload check")  # every ref


def test_a_gate_failure_warns_on_a_pr_and_fails_on_main(steps):
    run = step(steps, "Gates")["run"]
    assert 'if [ "$code" = "1" ] && [ "$MAIN_RUN" != "true" ]; then' in run and "::warning::" in run
    assert "GITHUB_STEP_SUMMARY" in run and 'exit "$code"' in run and "exit_code=$code" in run


def test_every_step_runs_in_the_image_through_the_one_script(steps):
    runs = [s["run"] for s in steps if "in_image.sh" in s.get("run", "")]
    assert len(runs) == 8 and not any("docker run" in s.get("run", "") for s in steps)
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.count("docker run") == 1 and "MLFLOW_TRACKING_URI" in script
    # --mount, not -v: a missing source fails instead of Docker creating a root-owned directory
    assert " -v " not in script and script.count("--mount") >= 9
    for source in ("labels.parquet", "features.parquet", "feature_catalog.csv", "dvc.lock"):
        assert re.search(rf"source=\S*{re.escape(source)},target=/app/\S*{re.escape(source)},readonly", script), source
    assert "source=$PWD/Model-Development/config,target=/app/Model-Development/config,readonly" in script
    for folder in ("runs", "mlruns", "reports"):
        assert f"source=$art/{folder},target=/app/Model-Development/{folder}" in script
    assert script.index('mkdir -p "${ARTIFACTS_DIR') < script.index("realpath")
    assert "--password" not in script and "TOKEN" not in script  # the image gets no credential


def test_the_change_filter_covers_the_data_inputs(workflow):
    run = workflow["jobs"]["changes"]["steps"][1]["run"]
    pattern = re.search(r"grep -qE '([^']+)'", run).group(1)
    for path in ("Model-Development/scripts/train.py", ".github/workflows/model.yml", ".github/scripts/in_image.sh",
                 "Data-Pipeline/dvc.lock", "Data-Pipeline/config/feature_catalog.csv"):
        assert re.search(pattern, path), path
    for path in ("README.md", "Data-Pipeline/scripts/alerts.py", "Data-Pipeline/config/processing.csv"):
        assert not re.search(pattern, path), path


def test_the_aggregate_check_logic_is_unchanged(workflow):
    run = workflow["jobs"]["model-ci"]["steps"][0]["run"]
    for text in ('[ "$CHANGES" != "success" ]', '[ "$PIPELINE" = "success" ]', '[ "$PIPELINE" = "skipped" ]'):
        assert text in run
    assert workflow["jobs"]["model-ci"]["if"] == "always()"


def test_the_failed_step_names_slack_gets_are_real_step_names(steps):
    notify = step(steps, "Notify Slack")["run"]
    names = [s.get("name", "").replace(" (main only)", "") for s in steps]
    for named in re.findall(r'check "([^"]+)"', notify):
        assert any(n.startswith(named) for n in names), named
    assert "--failed-step" in notify and "--gates-exit" in notify


# --- Google credentials only in the three main-only registry steps (ADR-035) --------------------

REGISTRY_STEPS = ("Re-score the champion (main only)", "Registry push (main only)", "Set champion (main only)")


def test_only_the_three_registry_steps_mount_credentials(workflow, steps):
    flagged = [s["name"] for s in steps if "MOUNT_GCP_CREDENTIALS" in (s.get("env") or {})]
    assert sorted(flagged) == sorted(REGISTRY_STEPS)
    assert all(step(steps, name)["env"]["MOUNT_GCP_CREDENTIALS"] == "1" for name in REGISTRY_STEPS)
    assert all(step(steps, name)["if"] == "env.MAIN_RUN == 'true'" for name in REGISTRY_STEPS)
    assert "MOUNT_GCP_CREDENTIALS" not in (workflow["jobs"]["pipeline"].get("env") or {})
    assert "MOUNT_GCP_CREDENTIALS" not in (workflow.get("env") or {})


def test_the_script_never_mounts_the_workspace():
    script = SCRIPT.read_text(encoding="utf-8")
    sources = re.findall(r"source=([^,\"]+)", script)
    assert sources and all(s != "$PWD" and s.rstrip("/") != "$PWD" for s in sources), sources
    assert all(not s.startswith("$PWD") or s.startswith(("$PWD/Data-Pipeline/", "$PWD/Model-Development/config"))
               for s in sources), sources


def test_the_credentials_file_stays_out_of_the_image():
    """.gitignore names it too; the compose test service does not mount .gitignore, so it is not read here."""
    ignore = (ROOT / "Model-Development" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert "gha-creds-*.json" in ignore and "**/gha-creds-*.json" in ignore


@pytest.fixture
def fake_docker(tmp_path):
    """in_image.sh run with a docker that records its arguments instead of starting a container."""
    import os
    import shutil
    import subprocess

    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    bin_dir, record = tmp_path / "bin", tmp_path / "docker_args.txt"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\n', encoding="utf-8")
    docker.chmod(0o755)

    def run(**env_changes):
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "IMAGE": "brt-train:test",
               "ARTIFACTS_DIR": str(tmp_path / "artifacts")}
        env.pop("MOUNT_GCP_CREDENTIALS", None)
        env.update(env_changes)
        record.unlink(missing_ok=True)
        done = subprocess.run(["bash", str(SCRIPT), "python", "-c", "pass"], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=60)
        args = record.read_text(encoding="utf-8").splitlines() if record.exists() else None
        return done, args

    return run


def test_no_credentials_without_the_flag(fake_docker, tmp_path):
    creds = tmp_path / "gha-creds-x.json"
    creds.write_text("{}", encoding="utf-8")
    done, args = fake_docker(GOOGLE_APPLICATION_CREDENTIALS=str(creds))  # exported by the auth step, flag unset
    assert done.returncode == 0, done.stderr
    assert not any("/run/gcp" in a or "gha-creds" in a or a.startswith("GOOGLE_APPLICATION_CREDENTIALS") for a in args)


def test_the_flag_mounts_the_file_read_only(fake_docker, tmp_path):
    creds = tmp_path / "gha-creds-x.json"
    creds.write_text("{}", encoding="utf-8")
    done, args = fake_docker(MOUNT_GCP_CREDENTIALS="1", GOOGLE_APPLICATION_CREDENTIALS=str(creds))
    assert done.returncode == 0, done.stderr
    assert f"type=bind,source={creds.resolve()},target=/run/gcp/credentials.json,readonly" in args
    assert "GOOGLE_APPLICATION_CREDENTIALS=/run/gcp/credentials.json" in args
    assert args.index("brt-train:test") > args.index("GOOGLE_APPLICATION_CREDENTIALS=/run/gcp/credentials.json")


@pytest.mark.parametrize("creds", ["", "missing.json"])
def test_the_flag_without_a_file_exits_2_and_runs_nothing(fake_docker, tmp_path, creds):
    done, args = fake_docker(MOUNT_GCP_CREDENTIALS="1",
                             GOOGLE_APPLICATION_CREDENTIALS=str(tmp_path / creds) if creds else "")
    assert done.returncode == 2 and args is None
    assert "names no file" in done.stderr
