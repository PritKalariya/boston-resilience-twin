"""Push DVC-tracked data to the team remote, or skip when it is out of reach.

The remote is gs://boston-resilience-twin-data/dvc (`.dvc/config`). Team
members and airflow-vm can write to it; anyone else, such as a grader,
cannot. Without Google credentials, or with credentials the bucket refuses,
the push is skipped with one log line and the run still succeeds: the data
stays in the local DVC cache. Any other failure fails the run.
"""

from __future__ import annotations

import argparse
import configparser
import logging
import subprocess
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Lowercase fragments of the GCS errors for "no access" (401 anonymous, 403
# denied), as gcsfs passes them through `dvc push`.
NO_ACCESS = ("does not have storage.", "permission 'storage.")
# A push that hangs (a stalled upload) fails the run instead of holding the task.
PUSH_TIMEOUT_SECONDS = 30 * 60

log = logging.getLogger(__name__)


def remote_url(repo: Path = REPO_ROOT) -> str:
    """The default remote's URL from `.dvc/config`, so the log lines follow the config."""
    config = configparser.ConfigParser()
    config.read(repo / ".dvc" / "config")
    name = config.get("core", "remote", fallback=None)
    section = f"'remote \"{name}\"'"
    if name is None or not config.has_option(section, "url"):
        return "the DVC remote"
    return config.get(section, "url")


def has_google_credentials() -> bool:
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    # A laptop login without a quota project warns here; Cloud Storage does not need one.
    # Without any credentials, google-auth also logs "Authentication failed using
    # Compute Engine authentication ..." as a warning; our own skip line says it better.
    default_log = logging.getLogger("google.auth._default")
    level = default_log.level
    default_log.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            try:
                google.auth.default()
            except DefaultCredentialsError:
                return False
        return True
    finally:
        default_log.setLevel(level)


def no_access_line(output: str) -> str | None:
    """The first line of DVC's output that says the credentials have no access, if any."""
    for line in output.splitlines():
        text = line.lower()
        if any(fragment in text for fragment in NO_ACCESS):
            return line.strip()
    return None


def is_no_access(output: str) -> bool:
    return no_access_line(output) is not None


def push(repo: Path = REPO_ROOT, run=None, credentials=None) -> bool:
    """Run `dvc push` in `repo`. True if pushed, False if skipped for lack of access."""
    run = run or subprocess.run
    credentials = credentials or has_google_credentials
    remote = remote_url(repo)
    if not credentials():
        log.warning(
            "dvc push skipped: no Google credentials (run `gcloud auth application-default login`). "
            "Data stays in the local DVC cache."
        )
        return False
    # The dvc that belongs to this Python, so a missing install fails with the
    # runbook's "No module named dvc" and not a bare FileNotFoundError.
    command = [sys.executable, "-m", "dvc", "push"]
    try:
        result = run(command, cwd=repo, capture_output=True, text=True, timeout=PUSH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"dvc push to {remote} did not finish in {PUSH_TIMEOUT_SECONDS // 60} minutes") from e
    output = result.stdout + result.stderr
    if result.returncode == 0:
        # stdout ends with DVC's summary ("1 file pushed"); stderr holds library warnings.
        summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "done"
        log.info("dvc push to %s: %s", remote, summary)
        return True
    reason = no_access_line(output)
    if reason:
        log.warning(
            "dvc push skipped: these credentials have no access to %s. Data stays in the local DVC cache. DVC said: %s",
            remote, reason,
        )
        return False
    raise RuntimeError(f"dvc push failed (exit {result.returncode}):\n{output.strip()}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    push(args.repo)


if __name__ == "__main__":
    main()
