"""Tests for the training stage, Model-Development/dvc.yaml (#107)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
import training_data as D  # noqa: E402

FOLDER = Path(__file__).resolve().parents[1]  # Model-Development, where dvc.yaml's paths start
SCRIPTS = FOLDER / "scripts"


def _stage() -> dict:
    return yaml.safe_load((FOLDER / "dvc.yaml").read_text(encoding="utf-8"))["stages"]["select"]


def _loads(script: str) -> set[str]:
    """The scripts in scripts/ that script imports, directly or through one another, itself included."""
    seen, todo = set(), [script]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(ast.parse((SCRIPTS / f"{name}.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module.split(".")[0]]
            else:
                continue
            todo.extend(name for name in names if (SCRIPTS / f"{name}.py").exists())
    return seen


def test_the_feature_catalog_is_a_dependency_of_the_training_stage():
    deps = _stage()["deps"]
    assert "../Data-Pipeline/config/feature_catalog.csv" in deps  # a #107 acceptance criterion
    assert {"../Data-Pipeline/data/processed/labels.parquet", "../Data-Pipeline/data/processed/features.parquet",
            "../Data-Pipeline/dvc.lock", "config"} <= set(deps)


def test_the_stage_depends_on_exactly_the_scripts_selection_loads():
    scripts = {dep for dep in _stage()["deps"] if dep.startswith("scripts/")}
    assert scripts == {f"scripts/{name}.py" for name in _loads("selection")}
    assert {"scripts/train.py", "scripts/models.py", "scripts/slices.py"} <= scripts


def test_the_stage_runs_the_selection_and_keeps_its_reports():
    stage = _stage()
    assert stage["cmd"] == "python scripts/selection.py" and stage["outs"] == ["reports"]
    assert D.load_settings().reports_dir == FOLDER / "reports"  # the out is where selection.py writes


def test_every_code_and_config_dependency_is_in_the_repo():
    for dep in _stage()["deps"]:
        if "/data/" not in dep:  # the data comes from dvc pull, not git
            assert (FOLDER / dep).exists(), dep
