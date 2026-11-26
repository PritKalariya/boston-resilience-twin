"""Checks on .github/workflows/deploy-serving.yml (#161, ADR-038). The workflow runs only after a merge, because
ci-writer can be used on main only (ADR-030), so these tests are what holds its shape before then. The mutation
tests prove each check fails on the mistake it is for."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
WORKFLOW = next(p for p in (HERE.parent / ".github" / "workflows" / "deploy-serving.yml",
                            HERE.parents[1] / ".github" / "workflows" / "deploy-serving.yml") if p.exists())


def triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True) or {}  # YAML 1.1 reads a bare `on` as True


def check_main_only(workflow: dict) -> list[str]:
    problems = []
    if set(triggers(workflow)) != {"workflow_dispatch"}:
        problems.append(f"triggers are {sorted(map(str, triggers(workflow)))}, not workflow_dispatch only")
    for name, job in workflow["jobs"].items():
        if str(job.get("if", "")).replace(" ", "") != "github.ref=='refs/heads/main'":
            problems.append(f"job {name} does not run on refs/heads/main only")
    return problems


def check_permissions(workflow: dict) -> list[str]:
    problems = []
    blocks = [("workflow", workflow.get("permissions"))] + [(n, j.get("permissions")) for n, j in workflow["jobs"].items()]
    for where, permissions in blocks:
        if not isinstance(permissions, dict):
            if where != "workflow":
                problems.append(f"{where} has no explicit permissions")
            continue
        for scope, level in permissions.items():
            if level == "write" and scope != "id-token":
                problems.append(f"{where} grants {scope}: write")
            if scope == "contents" and level != "read":
                problems.append(f"{where} grants contents: {level}")
    jobs = workflow["jobs"].values()
    if not all((j.get("permissions") or {}).get("id-token") == "write" for j in jobs):
        problems.append("a job lacks id-token: write, so it cannot authenticate")
    return problems


def check_shell_input(text: str) -> list[str]:
    return ["the input is spliced into a script"] if "${{ inputs." in text.replace("VERSION: ${{ inputs.version }}", "") else []


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text) -> dict:
    return yaml.safe_load(text)


def test_it_runs_on_main_only_and_has_no_pull_request_trigger(workflow):
    assert check_main_only(workflow) == []
    assert "pull_request" not in str(triggers(workflow))


def test_the_id_token_permission_is_the_only_write(workflow):
    assert check_permissions(workflow) == []


def test_two_deploys_never_overlap_and_the_job_has_a_timeout(workflow):
    assert workflow["concurrency"]["group"] and workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["deploy"]["timeout-minutes"] == 20


def find_workflow(name: str) -> Path | None:
    """A workflow file in a checkout; the test image holds only deploy-serving.yml."""
    return next((p for p in (HERE.parent / ".github" / "workflows" / name,
                             HERE.parents[1] / ".github" / "workflows" / name) if p.exists()), None)


def test_it_authenticates_as_ci_writer(workflow):
    step = next(s for s in workflow["jobs"]["deploy"]["steps"] if "google-github-actions/auth" in s.get("uses", ""))
    assert step["with"]["service_account"].startswith("ci-writer@")


def test_its_auth_step_is_model_ymls_ci_writer_step():
    model = find_workflow("model.yml")
    if model is None:
        pytest.skip("model.yml is not in the test image; the check runs in a checkout")
    reference = next(s for job in yaml.safe_load(model.read_text(encoding="utf-8"))["jobs"].values()
                     for s in job.get("steps", []) if s.get("id") == "auth_writer")
    mine = next(s for s in yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]
                if "google-github-actions/auth" in s.get("uses", ""))
    for key in ("workload_identity_provider", "service_account"):
        assert mine["with"][key] == reference["with"][key]
    assert mine["uses"] == reference["uses"]


def test_every_action_is_pinned_to_a_commit(workflow):
    for step in workflow["jobs"]["deploy"]["steps"]:
        if "uses" in step:
            assert len(step["uses"].split("@", 1)[1]) == 40, step["uses"]


def test_the_input_reaches_the_shell_through_an_environment_variable(workflow, text):
    assert check_shell_input(text) == []
    assert workflow["jobs"]["deploy"]["env"]["VERSION"] == "${{ inputs.version }}"
    run = next(s["run"] for s in workflow["jobs"]["deploy"]["steps"] if "deploy.py" in s.get("run", ""))
    assert '--version "$VERSION"' in run and "${{" not in run


def test_it_installs_the_deploy_requirements_under_the_lock(workflow):
    run = next(s["run"] for s in workflow["jobs"]["deploy"]["steps"] if "pip install" in s.get("run", ""))
    assert "-r Serving/requirements-deploy.txt" in run and "-c Model-Development/requirements.lock" in run


# ---- each check fails on the mistake it is for


def test_a_pull_request_trigger_is_caught(workflow):
    bad = copy.deepcopy(workflow)
    triggers(bad)["pull_request"] = {}
    assert check_main_only(bad)


def test_a_job_without_the_main_condition_is_caught(workflow):
    bad = copy.deepcopy(workflow)
    del bad["jobs"]["deploy"]["if"]
    assert check_main_only(bad)


def test_another_write_permission_is_caught(workflow):
    bad = copy.deepcopy(workflow)
    bad["jobs"]["deploy"]["permissions"]["packages"] = "write"
    assert check_permissions(bad)
    bad = copy.deepcopy(workflow)
    bad["jobs"]["deploy"]["permissions"]["contents"] = "write"
    assert check_permissions(bad)


def test_a_missing_id_token_is_caught(workflow):
    bad = copy.deepcopy(workflow)
    del bad["jobs"]["deploy"]["permissions"]["id-token"]
    assert check_permissions(bad)


def test_a_spliced_input_is_caught(text):
    assert check_shell_input(text + '\n      - run: echo "${{ inputs.version }}"\n')
