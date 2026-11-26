#!/usr/bin/env python3
"""Prepare a model folder for the model service from a local training run folder (#161).

  python3 Serving/scripts/local_model.py <run_dir> <out_dir>

<run_dir> is a run folder of Model-Development (model.joblib, run.json, ...). <out_dir> gets model.joblib
and a metadata.json with the fields the service reads, under the key names and nesting that
Model-Development/scripts/registry.py (build_push) writes, filled from the run folder's own files. It is the
folder to pass as the `model` build context (SERVE_MODEL_DIR in docker-compose.yaml). A real registry
metadata.json lists more files and keys; the service reads the same ones from it unchanged.

Standard library only, so it runs on the host's python3. It refuses an <out_dir> inside the repository: a
model is data and is never committed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODEL_FILE = "model.joblib"


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(data: dict) -> bytes:
    """registry.py's _json_bytes: the bytes a push uploads for metadata.json."""
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_metadata(run_dir: Path) -> dict:
    """metadata.json's content: only keys registry.py writes, from run.json and model.joblib."""
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read {run_dir / 'run.json'}: {exc}")
    mlflow_run_id = (run.get("mlflow") or {}).get("run_id")
    for name, value in (("model", run.get("model")), ("run_id", run.get("run_id")),
                        ("threshold", run.get("threshold")), ("mlflow.run_id", mlflow_run_id)):
        if value is None:
            sys.exit(f"{run_dir / 'run.json'} has no {name}")
    return {
        "model": run["model"], "run_id": run["run_id"], "data_version": run.get("data_version"),
        "mlflow_run_id": mlflow_run_id, "threshold": run["threshold"],
        "files": {MODEL_FILE: sha256(run_dir / MODEL_FILE)},
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args[:1] in (["-h"], ["--help"]) else 2
    run_dir, out_dir = Path(args[0]).resolve(), Path(args[1]).resolve()
    if out_dir == REPO or REPO in out_dir.parents:
        sys.exit(f"refusing to write inside the repository ({REPO}): pick a folder outside it, such as ~/brt-serve-model")
    if not (run_dir / MODEL_FILE).is_file():
        sys.exit(f"{run_dir / MODEL_FILE} is missing")
    metadata = build_metadata(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / MODEL_FILE, out_dir / MODEL_FILE)
    (out_dir / "metadata.json").write_bytes(json_bytes(metadata))
    for name in (MODEL_FILE, "metadata.json"):
        print(f"{sha256(out_dir / name)}  {out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
