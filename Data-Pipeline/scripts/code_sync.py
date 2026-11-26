"""Code sync on airflow-vm (#162): pull main, and rebuild the Airflow stack only when its inputs changed.

brt-code-sync.timer starts brt-code-sync.service at 17:30 America/New_York, before the
18:05 evening run. Deployment/terraform/startup.sh installs both units at boot (ADR-029,
dated note). The service runs this script on the VM host as root, with the host's
python3, so it uses the standard library only (alerts.py and credentials.py, which it
imports, do too).

1. Pull. As the airflow user that owns the checkout, `git pull --ff-only origin main`.
   The VM runs main only, so a checkout on another branch is an error. A pull that
   cannot fast-forward, or cannot reach GitHub, changes nothing: git leaves HEAD and the
   working tree as they were. Either way the script alerts and exits 1.
2. Plan. The files changed between the old and the new HEAD decide what else runs.
   Data-Pipeline/ is a directory mount (ADR-029), so the containers see new code at
   once. A change to the stack's definition or the Airflow image's inputs
   (docker-compose.yaml, airflow.Dockerfile and its .dockerignore, every file the
   Dockerfile copies, any requirements file) needs `docker compose up -d --build --wait`.
   A rebuild that changes nothing is cheap (the build is cached, and compose keeps every
   container whose image and settings are the same), while a missed one leaves new code
   on an old image. .dvcignore is a single-file mount (ADR-023): a container keeps the
   file it started with until it is recreated, so a change to it adds --force-recreate.
3. Rebuild, only when planned and only when it is safe. .env must exist and not be
   empty, since without it compose starts on its development defaults (ADR-034). No
   daily_pipeline run may be in progress, since recreating the scheduler kills its
   tasks (LocalExecutor, ADR-016). Otherwise the rebuild is postponed with an alert: the
   new code is on disk and runs on the old image until someone rebuilds by hand
   (runbook, "Update after a merge to main"). A successful build is followed by
   `docker image prune -f`, since old images would fill the 30 GB disk.
4. Alerts go to the journal, and to Slack when the repo's .env has SLACK_ALERT_WEBHOOK
   (the host has no such environment variable; startup.sh renders it from Secret
   Manager). The webhook URL is never printed.

    sudo systemctl start brt-code-sync.service     # run it now
    sudo journalctl -u brt-code-sync.service --no-pager
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import alerts

REPO = Path("/opt/boston-resilience-twin")  # startup.sh's CLONE_DIR
BRANCH = "main"
AIRFLOW_USER = "airflow"  # owns the checkout (#60)
DAG_ID = "daily_pipeline"
DOCKERFILE = "airflow.Dockerfile"
# The stack's definition and the Airflow image's build files (ADR-022). image_inputs adds
# the files the Dockerfile copies.
STACK_FILES = ("docker-compose.yaml", DOCKERFILE, f"{DOCKERFILE}.dockerignore")
FILE_MOUNTS = (".dvcignore",)  # single-file bind mounts in docker-compose.yaml
GIT_TIMEOUT_SECONDS = 120
CHECK_TIMEOUT_SECONDS = 120
# A build after a requirements change reinstalls packages. 30 minutes fits between the
# timer (17:30) and the evening run (18:05).
BUILD_TIMEOUT_SECONDS = 30 * 60

NONE, REBUILD, RECREATE = "none", "rebuild", "recreate"

Runner = Callable[..., subprocess.CompletedProcess]

log = logging.getLogger(__name__)


class SyncFailed(RuntimeError):
    """Something an operator must look at. The message is the alert."""


def copied_files(dockerfile_text: str) -> list[str]:
    """The build-context paths an image copies: the sources of each COPY and ADD, in
    shell or JSON form, without their --flags. A COPY --from=<stage> copies from another
    image, not from the repo, so it is left out."""
    text = re.sub(r"\\\r?\n", " ", dockerfile_text)  # join continued lines
    sources = []
    for line in text.splitlines():
        match = re.match(r"(?i)\s*(?:COPY|ADD)\s+(.+)", line)
        if not match:
            continue
        rest = match.group(1).strip()
        flags = []
        while rest.startswith("--"):
            flag, _, rest = rest.partition(" ")
            flags.append(flag)
            rest = rest.strip()
        if any(flag.startswith("--from") for flag in flags):
            continue
        if rest.startswith("["):
            try:
                parts = json.loads(rest)
            except json.JSONDecodeError:
                continue
        else:
            parts = shlex.split(rest)
        sources += [part[2:] if part.startswith("./") else part for part in parts[:-1]]
    return sources


def image_inputs(repo: Path) -> list[str]:
    """STACK_FILES plus what the pulled airflow.Dockerfile copies (paths or patterns)."""
    try:
        copied = copied_files((repo / DOCKERFILE).read_text(encoding="utf-8"))
    except OSError:
        copied = []
    return [*STACK_FILES, *copied]


def matches(path: str, pattern: str) -> bool:
    """A changed path matches a file, a glob or a copied directory; `.` is the whole context."""
    if pattern.strip("/") in ("", "."):
        return True
    return fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/")


def plan(changed: Iterable[str], inputs: Sequence[str] = STACK_FILES) -> str:
    """NONE, REBUILD or RECREATE (a rebuild that also recreates every container)."""
    changed = list(changed)
    if any(path in FILE_MOUNTS for path in changed):
        return RECREATE
    if any(Path(path).name.startswith("requirements") or any(matches(path, p) for p in inputs) for path in changed):
        return REBUILD
    return NONE


def first_line(text: str | None) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def last_line(text: str | None) -> str:
    return next((line.strip() for line in reversed((text or "").splitlines()) if line.strip()), "")


def as_airflow(*args: str) -> list[str]:
    """A command run as the checkout's owner, as startup.sh's as_airflow runs git."""
    return ["runuser", "-u", AIRFLOW_USER, "--", "env", "HOME=/nonexistent", *args]


def call(run: Runner, args: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    shown = shlex.join(args[args.index("git"):] if "git" in args else args)
    try:
        return run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SyncFailed(f"`{shown}` did not finish in {timeout // 60} minutes") from None
    except OSError as e:
        raise SyncFailed(f"could not run `{shown}`: {e}") from None


def git(repo: Path, run: Runner, *args: str) -> str:
    """stdout of a git command run as the airflow user; SyncFailed when it fails."""
    result = call(run, as_airflow("git", "-C", str(repo), *args), timeout=GIT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = first_line(result.stderr) or f"exit {result.returncode}"
        raise SyncFailed(f"git {' '.join(args)} failed: {reason}")
    return result.stdout.strip()


def pull(repo: Path, run: Runner) -> tuple[str, str]:
    """Fast-forward the checkout to origin/main. Returns the old and the new HEAD."""
    branch = git(repo, run, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise SyncFailed(f"the checkout is on {branch!r}, not {BRANCH}; the VM runs {BRANCH} only, so nothing was pulled")
    old = git(repo, run, "rev-parse", "HEAD")
    result = call(run, as_airflow("git", "-C", str(repo), "pull", "--ff-only", "--quiet", "origin", BRANCH),
                  timeout=GIT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = first_line(result.stderr) or f"exit {result.returncode}"
        try:
            now = git(repo, run, "rev-parse", "HEAD")
            where = f"the checkout is still at {old[:7]}" if now == old else f"the checkout moved to {now[:7]}"
        except SyncFailed:
            where = "the checkout's HEAD could not be read afterwards"
        raise SyncFailed(f"git pull --ff-only failed ({reason}); {where}")
    return old, git(repo, run, "rev-parse", "HEAD")


def env_ready(repo: Path) -> bool:
    try:
        return (repo / ".env").stat().st_size > 0
    except OSError:
        return False


def parse_runs(stdout: str) -> list[str]:
    """Run ids from `airflow dags list-runs ... -o json`, which prints "No data found"
    instead of an empty list, and may print warnings or log lines (which can start
    with `[` too) before the JSON."""
    text = stdout.strip()
    if not text or "No data found" in text:
        return []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            rows = json.loads("\n".join(lines[i:]))
        except json.JSONDecodeError:
            continue
        if isinstance(rows, list):
            return [str(row.get("run_id", "?")) if isinstance(row, dict) else "?" for row in rows]
    raise SyncFailed(f"could not read the running {DAG_ID} runs from: {first_line(text)}")


def running_runs(repo: Path, run: Runner) -> list[str]:
    args = ["docker", "compose", "exec", "-T", "airflow-scheduler",
            "airflow", "dags", "list-runs", DAG_ID, "--state", "running", "-o", "json"]
    result = call(run, args, cwd=repo, timeout=CHECK_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = last_line(result.stderr) or f"exit {result.returncode}"
        raise SyncFailed(f"could not check for a running {DAG_ID} run ({reason}); rebuild postponed")
    return parse_runs(result.stdout)


def rebuild(repo: Path, run: Runner, mode: str) -> None:
    if not env_ready(repo):
        raise SyncFailed(".env is missing or empty, and compose would start on its development defaults; "
                         "rebuild postponed (rerun the startup script first)")
    runs = running_runs(repo, run)
    if runs:
        raise SyncFailed(f"{DAG_ID} run {', '.join(runs)} is in progress, and recreating the scheduler "
                         "would kill its tasks; rebuild postponed")
    args = ["docker", "compose", "up", "-d", "--build", "--wait", *(["--force-recreate"] if mode == RECREATE else [])]
    result = call(run, args, cwd=repo, timeout=BUILD_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = last_line(result.stderr) or f"exit {result.returncode}"
        raise SyncFailed(f"`{shlex.join(args)}` failed ({reason}); the new code is on disk, "
                         "but the stack may still run the old image")
    pruned = call(run, ["docker", "image", "prune", "-f"], cwd=repo, timeout=CHECK_TIMEOUT_SECONDS)
    if pruned.returncode != 0:
        log.warning("docker image prune failed: %s", last_line(pruned.stderr) or f"exit {pruned.returncode}")


def webhook_from_env(repo: Path) -> str | None:
    """SLACK_ALERT_WEBHOOK from the repo's .env, whose lines are VAR='value' (startup.sh),
    or None when it is absent, empty or not a Slack incoming webhook."""
    try:
        lines = (repo / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() == alerts.SLACK_WEBHOOK_ENV:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == "'":
                value = value[1:-1].strip()
            return value if value.startswith(alerts.SLACK_WEBHOOK_PREFIX) else None
    return None


def alert(repo: Path, message: str, post: Callable[[str, str], None] | None = None) -> None:
    """Log the alert, and post it to Slack when .env has a webhook. Never raises."""
    log.error("ALERT %s", message)
    url = webhook_from_env(repo)
    if url is None:
        log.warning("no Slack webhook in %s; the alert is in the journal only", repo / ".env")
        return
    try:
        (post or alerts.post_to_slack)(url, f"brt-code-sync on airflow-vm: {message}")
        log.info("alert sent to Slack")
    except alerts.AlertNotSent as e:
        log.error("could not send the alert: %s", e)


def sync(repo: Path = REPO, run: Runner | None = None, post: Callable[[str, str], None] | None = None) -> int:
    """Pull, plan and rebuild. Returns 0, or 1 after an alert."""
    run = run or subprocess.run
    try:
        old, new = pull(repo, run)
        if old == new:
            log.info("%s is up to date at %s", BRANCH, new[:7])
            return 0
        changed = git(repo, run, "diff", "--name-only", old, new).splitlines()
        mode = plan(changed, image_inputs(repo))
        log.info("pulled %s..%s: %d files changed, %s", old[:7], new[:7], len(changed), mode)
        if mode != NONE:
            rebuild(repo, run, mode)
            log.info("stack rebuilt (%s)", mode)
        return 0
    except SyncFailed as e:
        alert(repo, str(e), post)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull main on airflow-vm and rebuild the Airflow stack when its inputs changed.")
    parser.add_argument("--repo", type=Path, default=REPO, help="the checkout (default: %(default)s)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="brt-code-sync: %(levelname)s %(message)s")
    return sync(args.repo)


if __name__ == "__main__":
    sys.exit(main())
