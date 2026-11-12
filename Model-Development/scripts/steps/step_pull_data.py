"""Step: check the mounted data files and record the data version (#112, ADR-028, ADR-032).

The files are pulled before this step, on the runner, by DVC with the CI service account
(`dvc pull` of the labels and the features); the image has no DVC repo and no credentials.
The workflow mounts them read-only: the labels and features parquet files, the feature
catalog and Data-Pipeline/dvc.lock, the four files training reads outside Model-Development
(training.csv: labels, features, feature_catalog, dvc_lock). This step checks each exists
and is readable (a parquet file must start with the PAR1 magic, so a text pointer or an
error page fails here, not in training), then writes `data_version.txt` with the same
function training uses, training_data.data_version: the sha256 of dvc.lock with LF line
endings. A missing or unreadable file exits 2.

Writes: `data_version.txt`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import step_common as C

PARQUET_MAGIC = b"PAR1"


def check_file(name: str, path: Path, parquet: bool) -> int:
    """The size of a readable, non-empty file; a StepError naming the setting otherwise."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(PARQUET_MAGIC))
        size = path.stat().st_size
    except OSError as exc:
        raise C.StepError(f"{name}: cannot read {path}: {exc}") from exc
    if size == 0:
        raise C.StepError(f"{name}: {path} is empty")
    if parquet and head != PARQUET_MAGIC:
        raise C.StepError(f"{name}: {path} is not a parquet file (does not start with PAR1)")
    return size


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(__doc__.splitlines()[0], argv, with_config=True)
    import training_data

    settings = C.load_settings(config)
    for name in ("labels", "features", "feature_catalog", "dvc_lock"):
        path = getattr(settings, name)
        size = check_file(name, path, parquet=name in ("labels", "features"))
        print(f"{name}: {path} ({size:,} bytes)")
    version = training_data.data_version(settings.dvc_lock)
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "data_version.txt").write_text(version + "\n", encoding="utf-8")
    print(f"data version {version}")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
