"""The packages that run the model are the ones it was trained with (Model-Development/requirements.lock)."""

import re
from importlib import metadata

from conftest import LOCK, REQUIREMENTS

PIN = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^\s;#]+)", re.MULTILINE)


def normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path) -> dict[str, str]:
    return {normal(name): version for name, version in PIN.findall(path.read_text(encoding="utf-8"))}


def test_every_installed_package_the_lock_pins_has_the_locks_version():
    locked = pins(LOCK)
    assert len(locked) > 80
    compared, wrong = 0, []
    for name, version in locked.items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue  # the training image has it, this one does not need it
        compared += 1
        if installed != version:
            wrong.append(f"{name}: installed {installed}, lock {version}")
    assert wrong == []
    assert compared > 30  # the service's own packages and their dependencies


def test_requirements_pin_exactly_and_agree_with_the_lock():
    locked = pins(LOCK)
    lines = [ln.strip() for ln in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert lines and all("==" in ln and not re.search(r"[<>~!]=?|\*", ln) for ln in lines), lines
    own = pins(REQUIREMENTS)
    assert {"fastapi", "lightgbm", "scikit-learn", "joblib", "jsonschema"} <= set(own)
    assert [name for name, version in own.items() if name in locked and locked[name] != version] == []


def test_the_service_installs_no_training_only_package():
    for name in ("mlflow", "optuna", "shap"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        raise AssertionError(f"{name} is installed in the serving image")
