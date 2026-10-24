"""Push DVC-tracked data to the team remote, or skip when it is out of reach.

The remote is gs://boston-resilience-twin-data/dvc (`.dvc/config`). Team
members and airflow-vm can write to it; anyone else, such as a grader,
cannot. Without Google credentials, or with credentials the bucket refuses,
the push is skipped with one log line and the run still succeeds: the data
stays in the local DVC cache. Any other failure fails the run.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REMOTE = "gs://boston-resilience-twin-data/dvc"
# Lowercase fragments of the GCS errors for "no access" (401 anonymous, 403
# denied), as gcsfs passes them through `dvc push`.
NO_ACCESS = ("does not have storage.", "permission 'storage.")

log = logging.getLogger(__name__)


def has_google_credentials() -> bool:
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    # A laptop login without a quota project warns here; Cloud Storage does not need one.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        try:
            google.auth.default()
        except DefaultCredentialsError:
            return False
    return True


def is_no_access(output: str) -> bool:
    text = output.lower()
    return any(fragment in text for fragment in NO_ACCESS)


def push(repo: Path = REPO_ROOT, run=subprocess.run, credentials=has_google_credentials) -> bool:
    """Run `dvc push` in `repo`. True if pushed, False if skipped for lack of access."""
    if not credentials():
        log.warning(
            "dvc push skipped: no Google credentials (run `gcloud auth application-default login`). "
            "Data stays in the local DVC cache."
        )
        return False
    result = run(["dvc", "push"], cwd=repo, capture_output=True, text=True)
    output = result.stdout + result.stderr
    if result.returncode == 0:
        # stdout ends with DVC's summary ("1 file pushed"); stderr holds library warnings.
        summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "done"
        log.info("dvc push to %s: %s", REMOTE, summary)
        return True
    if is_no_access(output):
        log.warning("dvc push skipped: these credentials have no access to %s. Data stays in the local DVC cache.", REMOTE)
        return False
    raise RuntimeError(f"dvc push failed (exit {result.returncode}):\n{output.strip()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    push(args.repo)


if __name__ == "__main__":
    main()
