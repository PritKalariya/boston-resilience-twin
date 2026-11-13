"""Tests for the dvc.lock code dependency check (#111). Each test builds its own git repository in
tmp_path: the test container does not mount the repository's .git."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lock_check as L  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A repository with a stage folder holding a tracked script, a tracked config folder, an
    untracked data file and a dvc.lock that matches all three."""
    root = tmp_path / "repo"
    stage = root / "Stage"
    (stage / "scripts").mkdir(parents=True)
    (stage / "config").mkdir()
    (stage / "data").mkdir()
    (stage / "scripts" / "step.py").write_bytes(b"print('step')\n")
    (stage / "config" / "a.csv").write_bytes(b"a,1\n")
    (stage / "config" / "b.csv").write_bytes(b"b,2\n")
    (stage / "data" / "table.parquet").write_bytes(b"data")
    git(root, "init", "-q")
    git(root, "add", "Stage/scripts", "Stage/config")
    write_lock(stage, {"scripts/step.py": md5(b"print('step')\n"), "config": L.dir_md5(stage / "config"),
                       "data/table.parquet": "0" * 32})
    git(root, "add", "Stage/dvc.lock")
    return root


def write_lock(stage: Path, deps: dict[str, str]) -> Path:
    lines = ["schema: '2.0'", "stages:", "  build:", "    cmd: python scripts/step.py", "    deps:"]
    for path, digest in deps.items():
        lines += [f"    - path: {path}", "      hash: md5", f"      md5: {digest}"]
    lock = stage / "dvc.lock"
    lock.write_text("\n".join(lines) + "\n")
    return lock


def test_a_matching_lock_passes_and_skips_data(repo):
    mismatches, checked = L.check([repo / "Stage" / "dvc.lock"], root=repo)
    assert mismatches == [] and checked == 2  # the script and the config folder; the data is DVC's


def test_an_edited_script_fails(repo):
    (repo / "Stage" / "scripts" / "step.py").write_bytes(b"print('changed')\n")
    mismatches, _ = L.check([repo / "Stage" / "dvc.lock"], root=repo)
    assert [(m.stage, m.path, m.actual) for m in mismatches] == [
        ("build", "Stage/scripts/step.py", md5(b"print('changed')\n"))]


def test_a_file_added_to_a_tracked_folder_fails(repo):
    (repo / "Stage" / "config" / "c.csv").write_bytes(b"c,3\n")
    mismatches, _ = L.check([repo / "Stage" / "dvc.lock"], root=repo)
    assert [m.path for m in mismatches] == ["Stage/config"]


def test_a_deleted_dependency_fails(repo):
    (repo / "Stage" / "scripts" / "step.py").unlink()
    mismatches, _ = L.check([repo / "Stage" / "dvc.lock"], root=repo)
    assert mismatches[0].actual is None


def test_dir_md5_is_dvcs_folder_hash(tmp_path):
    folder = tmp_path / "config"
    (folder / "sub").mkdir(parents=True)
    (folder / "b.csv").write_bytes(b"b")
    (folder / "sub" / "a.csv").write_bytes(b"a")
    listing = ('[{"md5": "' + md5(b"b") + '", "relpath": "b.csv"}, {"md5": "' + md5(b"a")
               + '", "relpath": "sub/a.csv"}]')
    assert L.dir_md5(folder) == md5(listing.encode()) + ".dir"


def test_the_committed_lock_folder_hash_is_reproduced():
    """The committed Model-Development/dvc.lock records Model-Development/config the way dir_md5 hashes it."""
    root = Path(__file__).resolve().parents[2]
    deps = {path: digest for _, path, digest in L.read_deps(root / "Model-Development" / "dvc.lock")}
    assert deps["config"] == L.dir_md5(root / "Model-Development" / "config")


def test_default_locks_come_from_registry_csv():
    assert [p.relative_to(L.ROOT).as_posix() for p in L.default_locks()] == [
        "Data-Pipeline/dvc.lock", "Model-Development/dvc.lock"]


@pytest.mark.parametrize("text, message", [
    ("not: [a, lock", "cannot read"),
    ("schema: '2.0'\n", "has no stages"),
    ("stages:\n  s:\n    deps:\n    - md5: x\n", "with no path"),
    ("stages:\n  s:\n    deps:\n    - path: x\n      hash: sha256\n", "has no md5"),
])
def test_unreadable_locks_are_errors(tmp_path, text, message):
    lock = tmp_path / "dvc.lock"
    lock.write_text(text)
    with pytest.raises(L.LockError, match=message):
        L.read_deps(lock)


def test_a_dependency_outside_the_repository_is_an_error(repo):
    write_lock(repo / "Stage", {"../../outside.py": "0" * 32})
    with pytest.raises(L.LockError, match="outside the repository"):
        L.check([repo / "Stage" / "dvc.lock"], root=repo)


def test_main_exit_codes(repo, monkeypatch, capsys):
    monkeypatch.setattr(L, "ROOT", repo)
    lock = str(repo / "Stage" / "dvc.lock")
    assert L.main([lock]) == 0
    assert "2 tracked dependencies match (Stage/dvc.lock)" in capsys.readouterr().out
    (repo / "Stage" / "scripts" / "step.py").write_bytes(b"changed")
    assert L.main([lock]) == 1
    captured = capsys.readouterr()
    assert "::error file=Stage/dvc.lock::" in captured.out
    assert "cd Stage && dvc repro --single-item build" in captured.err
    assert L.main([str(repo / "missing.lock")]) == 2
