"""Tests for .github/scripts/test_scope.py, which picks the test folders a pull request needs (#165).

The last test keeps its lists honest: every path one component's code, tests, config or DAGs
name in the other component's folder must be in READS_FROM_OTHER (or be shared, or be data,
which git does not hold), or a change there would skip tests that read it.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("test_scope", REPO / ".github" / "scripts" / "test_scope.py")
scope_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope_mod)
scope = scope_mod.scope

BOTH = ["Data-Pipeline", "Model-Development"]


@pytest.mark.parametrize(
    "changed, expected",
    [
        (["Model-Development/scripts/train.py"], ["Model-Development"]),
        (["Model-Development/README.md"], ["Model-Development"]),
        (["Data-Pipeline/scripts/daily_counts.py", "Data-Pipeline/tests/test_daily_counts.py"], ["Data-Pipeline"]),
        (["Data-Pipeline/scripts/daily_counts.py", "Model-Development/scripts/train.py"], BOTH),
        # A file the other component reads runs both.
        (["Model-Development/config/slice_groups.csv"], BOTH),
        (["Data-Pipeline/config/feature_catalog.csv"], BOTH),
        (["Data-Pipeline/dvc.lock"], BOTH),
        (["Data-Pipeline/scripts/alerts.py"], BOTH),
        # A shared file runs everything, whatever else changed.
        ([".github/workflows/ci.yml"], BOTH),
        (["docker-compose.yaml"], BOTH),
        (["airflow.Dockerfile"], BOTH),
        (["Data-Pipeline/requirements.txt"], BOTH),
        (["Data-Pipeline/tests/run_tests.sh"], BOTH),
        (["docs/data/category-map.csv"], BOTH),
        (["docs/runbook.md", ".coveragerc"], BOTH),
        # Nothing the tests can see: no tests.
        (["docs/runbook.md"], []),
        (["Deployment/terraform-project2/iam.tf", "README.md"], []),
        (["Serving/app.py"], []),
        ([], []),
    ],
)
def test_scope(changed, expected):
    assert scope(changed) == expected


def test_shared_prefix_needs_its_slash():
    # ".github/" matches files under it only; "docs/data/" does not match "docs/database.md".
    assert scope(["docs/database.md"]) == []
    assert scope([".githubx"]) == []


def test_main_all_and_output(tmp_path, monkeypatch, capsys):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert scope_mod.main(["--all", "--github-output"]) == 0
    assert capsys.readouterr().out.strip() == "Data-Pipeline Model-Development"
    assert out.read_text() == "suites=Data-Pipeline Model-Development\n"


def test_main_diff(monkeypatch, capsys):
    monkeypatch.setattr(scope_mod, "changed_files", lambda base, head: ["Model-Development/scripts/train.py"])
    assert scope_mod.main(["--base", "a", "--head", "b"]) == 0
    assert capsys.readouterr().out.strip() == "Model-Development"


def test_main_failed_diff_runs_everything(monkeypatch, capsys):
    def fail(base, head):
        raise scope_mod.subprocess.CalledProcessError(128, ["git"], stderr="bad revision")

    monkeypatch.setattr(scope_mod, "changed_files", fail)
    assert scope_mod.main(["--base", "a", "--head", "b"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "Data-Pipeline Model-Development"
    assert "bad revision" in captured.err


def test_main_needs_base_and_head():
    with pytest.raises(SystemExit):
        scope_mod.main(["--base", "a"])


def _cross_references(component: str, other: str) -> set[str]:
    """Paths in `other` that files of `component` name, as "Other/a/b" or "Other" / "a" / "b"."""
    literal = re.compile(re.escape(other) + r"/[\w./-]+")
    joined = re.compile(r"""["']""" + re.escape(other) + r"""["']((?:\s*/\s*["'][\w.-]+["'])+)""")
    found = set()
    for folder in ("scripts", "tests", "config", "dags", "registry"):
        for path in (REPO / component / folder).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".csv", ".yaml", ".yml", ".sh"}:
                continue
            # run_tests.sh is shared (a change to it runs every test), and this file names
            # the other folder's paths only as examples.
            if path.name in {"run_tests.sh", Path(__file__).name}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            found.update(m.rstrip(".") for m in literal.findall(text))
            for parts in joined.findall(text):
                found.add(other + "/" + "/".join(re.findall(r"[\w.-]+", parts)))
    return found


@pytest.mark.parametrize("component, other", [("Data-Pipeline", "Model-Development"),
                                              ("Model-Development", "Data-Pipeline")])
def test_every_cross_folder_read_is_listed(component, other):
    listed = scope_mod.READS_FROM_OTHER[component]
    missing = sorted(
        ref for ref in _cross_references(component, other)
        if not ref.startswith(f"{other}/data/")  # not in git: nothing in a diff to miss
        and ref not in {f"{other}/tests", f"{other}/scripts"}  # prose naming a folder
        and not scope_mod._matches(ref, scope_mod.SHARED)
        and not scope_mod._matches(ref, listed)
    )
    assert not missing, (
        f"{component} reads {missing} from {other}; add them to READS_FROM_OTHER['{component}'] "
        "in .github/scripts/test_scope.py so a change there runs its tests"
    )
