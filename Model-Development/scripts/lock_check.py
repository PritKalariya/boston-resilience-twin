"""Check the code dependencies recorded in dvc.lock files against the tree (#111).

    python Model-Development/scripts/lock_check.py [LOCK ...]

A model version records its data version, the sha256 of Data-Pipeline/dvc.lock (ADR-027). That is
only true to the code if each lock still matches the files it names: a script edited after the last
`dvc repro` leaves a lock that says the data came from code that is no longer in the tree. This
checks every stage dependency that git tracks (scripts, config files and folders, other locks)
against its md5 in the lock, as DVC 3 computes it: the md5 of the file's bytes, and for a folder the
md5 of the JSON list of its files' md5 and relpath (dvc_data). Data dependencies are not in git (DVC
holds them) and are not checked: CI has no data.

With no LOCK, the dvc_locks of Model-Development/registry/registry.csv are checked. Paths in a lock
are relative to the lock's folder. Run it on an LF checkout, as `dvc repro` is (CLAUDE.md).

Exit codes: 0 every tracked dependency matches, 1 one or more do not (each is named, with the
command that refreshes the lock), 2 a lock cannot be read.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DIR_SUFFIX = ".dir"


class LockError(ValueError):
    """A lock file that cannot be read."""


@dataclass(frozen=True)
class Mismatch:
    lock: Path
    stage: str
    path: str
    recorded: str
    actual: str | None  # None: the dependency is no longer in the tree


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dir_md5(path: Path, tracked: set[str] | None = None, from_root: str = "") -> str:
    """DVC's folder hash: the md5 of json.dumps(sorted [{md5, relpath}], sort_keys=True), plus .dir.

    With tracked (git ls-files paths) and the folder's path from the repo root, only git-tracked files
    count: the folder as a CI checkout has it, so a local __pycache__ or scratch file is no mismatch."""
    files = [f for f in path.rglob("*") if f.is_file()]
    if tracked is not None:
        files = [f for f in files if f"{from_root.rstrip('/')}/{f.relative_to(path).as_posix()}" in tracked]
    files.sort(key=lambda f: f.relative_to(path).as_posix())
    entries = [{"md5": file_md5(f), "relpath": f.relative_to(path).as_posix()} for f in files]
    return hashlib.md5(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest() + DIR_SUFFIX


def tracked_files(root: Path) -> set[str]:
    """Every path git tracks, relative to root, as posix."""
    try:
        done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LockError(f"git ls-files failed in {root}: {exc}") from None
    return {name for name in done.stdout.decode("utf-8").split("\0") if name}


def _is_tracked(relpath: str, tracked: set[str]) -> bool:
    return relpath in tracked or any(name.startswith(relpath.rstrip("/") + "/") for name in tracked)


def read_deps(lock: Path) -> list[tuple[str, str, str]]:
    """(stage, path relative to the lock's folder, recorded md5) for every stage dependency."""
    try:
        data = yaml.safe_load(lock.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LockError(f"cannot read {lock}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("stages"), dict):
        raise LockError(f"{lock} has no stages")
    deps = []
    for stage, body in data["stages"].items():
        for dep in (body or {}).get("deps", []) or []:
            if not isinstance(dep, dict) or "path" not in dep:
                raise LockError(f"{lock}: stage {stage} has a dependency with no path")
            if dep.get("hash", "md5") != "md5" or "md5" not in dep:
                raise LockError(f"{lock}: stage {stage} dependency {dep['path']} has no md5")
            deps.append((stage, dep["path"], str(dep["md5"])))
    return deps


def check(locks: list[Path], root: Path | None = None) -> tuple[list[Mismatch], int]:
    """The mismatches over every tracked dependency of locks, and how many dependencies were checked."""
    root = Path(root or ROOT)
    tracked = tracked_files(root)
    mismatches, checked = [], 0
    for lock in locks:
        lock = Path(lock).resolve()
        for stage, relpath, recorded in read_deps(lock):
            path = (lock.parent / relpath).resolve()
            try:
                from_root = path.relative_to(root.resolve()).as_posix()
            except ValueError:
                raise LockError(f"{lock}: stage {stage} dependency {relpath} is outside the repository") from None
            if not _is_tracked(from_root, tracked):
                continue  # data: DVC holds it, and CI has none
            checked += 1
            if recorded.endswith(DIR_SUFFIX):
                actual = dir_md5(path, tracked, from_root) if path.is_dir() else None
            else:
                actual = file_md5(path) if path.is_file() else None
            if actual != recorded:
                mismatches.append(Mismatch(lock, stage, from_root, recorded, actual))
    return mismatches, checked


REGISTRY_CONFIG = ROOT / "Model-Development" / "registry" / "registry.csv"


def default_locks(config: Path = REGISTRY_CONFIG) -> list[Path]:
    """registry.csv's dvc_locks, read with the standard library only: CI runs this check with no
    project packages installed (registry.load_settings needs pandas through training_data)."""
    try:
        rows = list(csv.reader(config.read_text(encoding="utf-8").splitlines()))
    except OSError as exc:
        raise LockError(f"cannot read {config}: {exc}") from None
    values = [row[1].strip() for row in rows[1:] if len(row) > 1 and row[0].strip() == "dvc_locks"]
    if len(values) != 1 or not values[0]:
        raise LockError(f"{config.name} needs one dvc_locks setting")
    return [ROOT / part.strip() for part in values[0].split(";") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("locks", nargs="*", type=Path, help="dvc.lock files (default: registry.csv dvc_locks)")
    args = parser.parse_args(argv)
    try:
        locks = args.locks or default_locks()
        mismatches, checked = check(locks)
    except (LockError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    names = ", ".join(Path(lock).resolve().relative_to(ROOT).as_posix() for lock in locks)
    if not mismatches:
        print(f"lock check: {checked} tracked dependencies match ({names})")
        return 0
    for m in mismatches:
        lock = m.lock.relative_to(ROOT).as_posix()
        now = "missing from the tree" if m.actual is None else f"tree {m.actual}"
        print(f"::error file={lock}::{lock} stage {m.stage}: {m.path} is {now}, lock records {m.recorded}")
    stale = sorted({(m.lock.parent.relative_to(ROOT).as_posix(), m.stage) for m in mismatches})
    print(f"lock check: {len(mismatches)} of {checked} tracked dependencies do not match. Refresh the lock from an "
          "LF checkout and commit it: " + "; ".join(f"cd {folder} && dvc repro --single-item {stage}"
                                                    for folder, stage in stale), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
