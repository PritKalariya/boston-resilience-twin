"""Checks on .github/workflows/ci.yml and component-ci.yml (#159, ADR-037).

A new component adds a caller job to ci.yml; these tests keep the `ci` aggregator in step with
the jobs, the callers complete, and the reusable workflow's bar fixed at 70 with no network and
no cloud token. The compose test service mounts .github read-only, so they run in CI's job too.
The mutation tests prove each check fails on the mistake it is for.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
COMPONENT_CI = ROOT / ".github" / "workflows" / "component-ci.yml"
REUSABLE = "./.github/workflows/component-ci.yml"


def check_aggregator(ci: dict) -> list[str]:
    jobs = ci["jobs"]
    problems = []
    if "ci" not in jobs:
        return ["no `ci` aggregator job"]
    needs = jobs["ci"].get("needs", [])
    needs = [needs] if isinstance(needs, str) else list(needs)
    missing = sorted(set(jobs) - {"ci"} - set(needs))
    if missing:
        problems.append(f"jobs missing from ci.needs: {missing}")
    unknown = sorted(set(needs) - set(jobs))
    if unknown:
        problems.append(f"ci.needs names jobs that do not exist: {unknown}")
    if str(jobs["ci"].get("if", "")).strip() != "always()":
        problems.append("ci must have `if: always()`")
    return problems


def check_callers(ci: dict) -> list[str]:
    problems = []
    for name, job in ci["jobs"].items():
        if job.get("uses") == REUSABLE:
            for key in ("component", "cov_source"):
                if not (job.get("with") or {}).get(key):
                    problems.append(f"job {name} does not pass `{key}`")
    return problems


def check_reusable(text: str) -> list[str]:
    problems = []
    if not re.search(r"--cov-fail-under=70(\s|$)", text):
        problems.append("the bar is not --cov-fail-under=70")
    if "--network none" not in text:
        problems.append("no --network none")
    workflow = yaml.safe_load(text)
    permissions = [workflow.get("permissions")] + [j.get("permissions") for j in workflow["jobs"].values()]
    if any("id-token" in (p or {}) for p in permissions) or "id-token" in text:
        problems.append("an id-token permission is present")
    return problems


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def component_ci_text() -> str:
    return COMPONENT_CI.read_text(encoding="utf-8")


def test_the_aggregator_needs_every_other_job_and_always_runs(ci):
    assert check_aggregator(ci) == []


def test_every_caller_passes_its_component_and_cov_source(ci):
    callers = [n for n, j in ci["jobs"].items() if j.get("uses") == REUSABLE]
    assert "component-template" in callers
    assert check_callers(ci) == []


def test_the_reusable_workflow_fixes_the_bar_and_has_no_network_or_token(component_ci_text):
    assert check_reusable(component_ci_text) == []


def test_the_bar_is_not_an_input(component_ci_text):
    inputs = yaml.safe_load(component_ci_text)[True]["workflow_call"]["inputs"]
    assert set(inputs) == {"component", "context", "cov_source", "dockerfile"}
    assert not [name for name in inputs if re.search(r"bar|under|cov_fail|threshold", name)]


def test_the_existing_job_keeps_its_name_and_the_template_runs_in_parallel(ci):
    assert ci["jobs"]["lint-and-test"]["name"] == "Lint and test (Python 3.12)"
    assert "needs" not in ci["jobs"]["component-template"]


def test_the_reusable_workflow_is_read_only_and_pinned():
    text = COMPONENT_CI.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["permissions"] == {"contents": "read"}
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses


# Mutations: each check must fail on the mistake it exists for.


def test_mutation_a_job_missing_from_needs_fails(ci):
    bad = copy.deepcopy(ci)
    bad["jobs"]["ci"]["needs"] = [n for n in bad["jobs"]["ci"]["needs"] if n != "component-template"]
    assert check_aggregator(bad) == ["jobs missing from ci.needs: ['component-template']"]


def test_mutation_a_new_job_not_in_needs_fails(ci):
    bad = copy.deepcopy(ci)
    bad["jobs"]["mutation-probe"] = {"uses": REUSABLE, "with": {"component": "Probe", "cov_source": "probe"}}
    assert any("mutation-probe" in p for p in check_aggregator(bad))


def test_mutation_an_aggregator_without_always_fails(ci):
    bad = copy.deepcopy(ci)
    del bad["jobs"]["ci"]["if"]
    assert check_aggregator(bad) == ["ci must have `if: always()`"]


def test_mutation_a_caller_without_cov_source_fails(ci):
    bad = copy.deepcopy(ci)
    del bad["jobs"]["component-template"]["with"]["cov_source"]
    assert check_callers(bad) == ["job component-template does not pass `cov_source`"]


def test_mutation_the_bar_set_to_60_fails(component_ci_text):
    assert check_reusable(component_ci_text.replace("--cov-fail-under=70", "--cov-fail-under=60"))


def test_mutation_network_allowed_fails(component_ci_text):
    assert check_reusable(component_ci_text.replace("--network none", "--network host"))


def test_mutation_an_id_token_permission_fails(component_ci_text):
    bad = component_ci_text.replace("permissions:\n  contents: read\n", "permissions:\n  contents: read\n  id-token: write\n", 1)
    assert bad != component_ci_text and check_reusable(bad)
