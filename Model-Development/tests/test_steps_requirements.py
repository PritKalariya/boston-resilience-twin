"""The training image's requirements match the pins the tests pass on (#112, ADR-032).

Model-Development/requirements.txt (the image) and requirements-pull.txt (the runner's DVC) copy
the pins of the packages they share with Data-Pipeline/requirements.txt. A package pinned in only
one of the two files is allowed (the image may add one, such as google-cloud-aiplatform for #111);
a package pinned in both must have equal pins. These tests read the files from the repo only, so
they run in the compose test container, which mounts both folders.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "Model-Development"
SHARED = ROOT / "Data-Pipeline" / "requirements.txt"
IMAGE_FILE, PULL_FILE = MODEL / "requirements.txt", MODEL / "requirements-pull.txt"
LOCK_FILE = MODEL / "requirements.lock"  # pip freeze of the image registry version 1 records (#111)
IMPORT_NAMES = {"sklearn": "scikit-learn", "yaml": "pyyaml", "google": "google-auth"}  # import name -> package name, where they differ


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


def mismatched_shared_pins(shared_path: Path, own_path: Path) -> list[str]:
    """One message per package pinned in both files with different pins; a package in one file only is fine."""
    shared, own = pins(shared_path), pins(own_path)
    return [f"{name}: {own[name]} in {own_path.name}, {shared[name]} in {shared_path.name}"
            for name in sorted(own.keys() & shared.keys()) if own[name] != shared[name]]


@pytest.mark.parametrize("path", [IMAGE_FILE, PULL_FILE])
def test_every_shared_pin_matches_data_pipeline(path):
    assert pins(path)
    assert mismatched_shared_pins(SHARED, path) == []


def test_an_image_only_package_passes_and_a_mismatched_shared_pin_fails(tmp_path):
    shared, own = tmp_path / "shared.txt", tmp_path / "own.txt"
    shared.write_text("numpy==2.5.3\npandas==3.0.5\nairflow-only==1.0.0\n", encoding="utf-8")
    own.write_text("numpy==2.5.3\ngoogle-cloud-aiplatform==1.2.3  # image only (#111)\n", encoding="utf-8")
    assert mismatched_shared_pins(shared, own) == []  # image-only and shared-only packages are allowed
    own.write_text("numpy==2.5.4\ngoogle-cloud-aiplatform==1.2.3\n", encoding="utf-8")
    assert mismatched_shared_pins(shared, own) == ["numpy: ('2.5.4; ',) in own.txt, ('2.5.3; ',) in shared.txt"]


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


def lock_mismatches(image_path: Path, lock_path: Path) -> list[str]:
    """One message per direct package of the Linux image whose pin is missing from the lock or differs
    from it. A requirements.txt line with a marker other than linux is skipped: the image is Linux."""
    lock = pins(lock_path)
    problems = []
    for name, values in sorted(pins(image_path).items()):
        for value in values:
            version, _, marker = value.partition("; ")
            if marker and marker != 'sys_platform == "linux"':
                continue
            if name not in lock:
                problems.append(f"{name}=={version} is in {image_path.name} but not in {lock_path.name}")
            elif lock[name] != (f"{version}; ",):
                problems.append(f"{name}: {version} in {image_path.name}, {lock[name]} in {lock_path.name}")
    return problems


def test_the_lock_agrees_with_every_direct_pin():
    """The Dockerfile installs requirements.txt with -c requirements.lock: a pin that disagrees fails the build."""
    lock = pins(LOCK_FILE)
    assert all(len(values) == 1 and not values[0].split("; ")[1] for values in lock.values())  # one plain pin each
    assert lock_mismatches(IMAGE_FILE, LOCK_FILE) == []
    assert "scipy" in lock  # a transitive package: the reason the lock exists
    assert "xgboost" not in lock and lock["xgboost-cpu"] == ("3.4.1; ",)  # frozen on Linux


def test_a_changed_or_missing_direct_pin_is_named(tmp_path):
    image, lock = tmp_path / "requirements.txt", tmp_path / "requirements.lock"
    image.write_text('numpy==2.5.3\nxgboost==3.4.1; sys_platform != "linux"\n'
                     'xgboost-cpu==3.4.1; sys_platform == "linux"\n', encoding="utf-8")
    lock.write_text("# header\nnumpy==2.5.3\nscipy==1.18.1\nxgboost-cpu==3.4.1\n", encoding="utf-8")
    assert lock_mismatches(image, lock) == []  # the non-Linux xgboost line is skipped
    image.write_text("numpy==2.5.4\nshap==0.54.1\n", encoding="utf-8")
    assert lock_mismatches(image, lock) == [
        "numpy: 2.5.4 in requirements.txt, ('2.5.3; ',) in requirements.lock",
        "shap==0.54.1 is in requirements.txt but not in requirements.lock",
    ]


def instructions(dockerfile: str) -> list[list[str]]:
    """The Dockerfile's instructions as word lists: comments dropped, backslash continuations joined."""
    joined = re.sub(r"\\\n", " ", "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")))
    return [line.split() for line in joined.splitlines() if line.strip()]


def lock_problems(dockerfile: str) -> list[str]:
    """What is wrong with the Dockerfile's use of the lock: the pip install must take it with -c, and a COPY
    before that install must bring it in."""
    steps = instructions(dockerfile)
    installs = [i for i, words in enumerate(steps) if words[:3] == ["RUN", "pip", "install"]]
    if len(installs) != 1:
        return [f"{len(installs)} RUN pip install instructions, expected 1"]
    words, problems = steps[installs[0]], []
    if not any(a == "-c" and b == "Model-Development/requirements.lock" for a, b in zip(words, words[1:])):
        problems.append("the RUN pip install has no -c Model-Development/requirements.lock")
    if not any(w[0] == "COPY" and any(x.endswith("Model-Development/requirements.lock") for x in w) for w in steps[: installs[0]]):
        problems.append("requirements.lock is not copied before the RUN pip install")
    return problems


def test_the_dockerfile_installs_with_the_lock():
    assert lock_problems((MODEL / "Dockerfile").read_text(encoding="utf-8")) == []


def test_the_lock_must_be_on_the_install_line_itself():
    copy = "COPY Model-Development/requirements.txt Model-Development/requirements.lock /app/Model-Development/\n"
    install = "RUN pip install --no-cache-dir -r Model-Development/requirements.txt \\\n    -c Model-Development/requirements.lock \\\n    && pip check\n"
    assert lock_problems(copy + install) == []
    commented = copy + "# pip install -c Model-Development/requirements.lock\nRUN pip install -r Model-Development/requirements.txt && pip check\n"
    assert lock_problems(commented) == ["the RUN pip install has no -c Model-Development/requirements.lock"]
    assert lock_problems(install + copy) == ["requirements.lock is not copied before the RUN pip install"]


def header_problems(lock_path: Path, dockerfile: str) -> list[str]:
    """The lock header's machine-written lines against the file: '# packages:' must count the pinned lines and
    '# base:' must name the Dockerfile's FROM image, so neither goes stale unseen."""
    text = lock_path.read_text(encoding="utf-8")
    fields = dict(re.findall(r"^# (source|base|packages): (.+)$", text, flags=re.M))
    problems = [f"no '# {key}:' line" for key in ("source", "base", "packages") if key not in fields]
    pinned = sum(1 for line in text.splitlines() if line.strip() and not line.startswith("#"))
    if "packages" in fields and fields["packages"] != str(pinned):
        problems.append(f"the header says {fields['packages']} packages, the file pins {pinned}")
    (base,) = [words[1] for words in instructions(dockerfile) if words[0] == "FROM"]
    if "base" in fields and fields["base"].split(",")[0] != base:
        problems.append(f"the header's base {fields['base'].split(',')[0]} is not the Dockerfile's {base}")
    return problems


def test_the_lock_header_matches_the_file():
    assert header_problems(LOCK_FILE, (MODEL / "Dockerfile").read_text(encoding="utf-8")) == []


def test_a_stale_lock_header_is_named(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("# source: test\n# base: python:3.12.13-slim@sha256:aa, linux/amd64\n# packages: 2\nnumpy==2.5.3\nscipy==1.18.1\n",
                    encoding="utf-8")
    assert header_problems(lock, "FROM python:3.12.13-slim@sha256:aa\n") == []
    assert header_problems(lock, "FROM python:3.12.13-slim@sha256:bb\n") == [
        "the header's base python:3.12.13-slim@sha256:aa is not the Dockerfile's python:3.12.13-slim@sha256:bb"]
    lock.write_text(lock.read_text(encoding="utf-8") + "shap==0.54.1\n", encoding="utf-8")
    assert header_problems(lock, "FROM python:3.12.13-slim@sha256:aa\n") == ["the header says 2 packages, the file pins 3"]
    lock.write_text("numpy==2.5.3\n", encoding="utf-8")
    assert header_problems(lock, "FROM python:3.12.13-slim@sha256:aa\n") == ["no '# source:' line", "no '# base:' line", "no '# packages:' line"]


def test_the_base_image_is_pinned_by_digest():
    (base,) = [words[1] for words in instructions((MODEL / "Dockerfile").read_text(encoding="utf-8")) if words[0] == "FROM"]
    assert re.fullmatch(r"python:3\.12\.13-slim@sha256:[0-9a-f]{64}", base), base  # a rebuilt tag cannot move the image (#111)
