"""The training image's requirements match the pins the tests pass on (#112, ADR-032).

Model-Development/requirements.txt (the image) and requirements-pull.txt (the runner's DVC) copy
their pins from Data-Pipeline/requirements.txt. These tests read the files from the repo only, so
they run in the compose test container, which mounts both folders.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "Model-Development"
SHARED = ROOT / "Data-Pipeline" / "requirements.txt"
IMAGE_FILE, PULL_FILE = MODEL / "requirements.txt", MODEL / "requirements-pull.txt"
IMPORT_NAMES = {"sklearn": "scikit-learn", "yaml": "pyyaml"}  # import name -> package name, where they differ


def pins(path: Path) -> dict[str, tuple[str, ...]]:
    """package (lower case, - for _) -> its pins as 'version; marker' strings; options and comments skipped."""
    found: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement, _, marker = line.partition(";")
        name, equals, version = requirement.partition("==")
        assert equals, f"{path.name}: {line!r} is not pinned with =="
        found.setdefault(name.strip().lower().replace("_", "-"), []).append(f"{version.strip()}; {marker.strip()}")
    return {name: tuple(values) for name, values in found.items()}


@pytest.mark.parametrize("path", [IMAGE_FILE, PULL_FILE])
def test_every_pin_matches_data_pipeline(path):
    shared, own = pins(SHARED), pins(path)
    assert own
    for name, values in own.items():
        assert name in shared, f"{name} is pinned in {path.name} but not in Data-Pipeline/requirements.txt"
        assert values == shared[name], f"{name}: {values} in {path.name}, {shared[name]} in Data-Pipeline"


def test_the_xgboost_platform_markers_are_kept():
    assert pins(IMAGE_FILE)["xgboost-cpu"] == ('3.4.1; sys_platform == "linux"',) == pins(SHARED)["xgboost-cpu"]
    assert pins(IMAGE_FILE)["xgboost"] == ('3.4.1; sys_platform != "linux"',)


def test_the_image_file_has_no_constraints_or_includes():
    options = [line for line in IMAGE_FILE.read_text(encoding="utf-8").splitlines() if line.strip().startswith("-")]
    assert options == []  # no -c Airflow constraints URL, no -r (ADR-032)


def test_the_pull_file_is_dvc_and_its_google_remote_only():
    assert sorted(pins(PULL_FILE)) == ["dvc", "dvc-gs"]
    assert not {"dvc", "dvc-gs"} & set(pins(IMAGE_FILE))  # the image has no DVC and no credentials


def test_every_third_party_import_of_the_scripts_is_in_the_image_file():
    local = {path.stem for folder in (MODEL / "scripts", MODEL / "scripts" / "steps") for path in folder.glob("*.py")}
    wanted = set()
    for folder in (MODEL / "scripts", MODEL / "scripts" / "steps"):
        for path in folder.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                           else [])
                for module in modules:
                    top = module.split(".")[0]
                    if top not in sys.stdlib_module_names and top not in local and top != "__future__":
                        wanted.add(IMPORT_NAMES.get(top, top).lower().replace("_", "-"))
    missing = sorted(wanted - set(pins(IMAGE_FILE)) - {"xgboost"} - ({"xgboost"} if "xgboost-cpu" in pins(IMAGE_FILE) else set()))
    assert missing == [], f"imported by the scripts but not in Model-Development/requirements.txt: {missing}"
    assert {"lightgbm", "mlflow", "scikit-learn", "pandas", "pyarrow", "joblib", "pyyaml", "matplotlib"} <= wanted
