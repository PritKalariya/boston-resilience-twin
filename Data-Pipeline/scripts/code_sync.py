"""Code sync on airflow-vm (#162): pull main, and rebuild the Airflow stack when its inputs changed.

brt-code-sync.timer starts brt-code-sync.service at 17:30 America/New_York, before the 18:05 evening
run. Deployment/terraform/startup.sh installs both units at boot (ADR-029, dated note). The service
runs this script on the VM host as root, with the host's python3, so it uses the standard library
only (alerts.py and credentials.py, which it imports, do too).

1. Wait for a running run. If a daily_pipeline run is in progress, the whole sync waits for the next
   17:30, with an alert: Data-Pipeline/ is a directory mount (ADR-029), so a pull would change the
   code under the run's later tasks. When the scheduler is not up, nothing is running and the sync
   goes on. When the runs cannot be read, the sync waits too.
2. Pull. As the airflow user that owns the checkout, `git pull --ff-only origin main`. The VM runs
   main only, so a checkout on another branch is an error. A pull that cannot fast-forward, or cannot
   reach GitHub, changes nothing: git leaves HEAD and the working tree as they were. Either way the
   script alerts and exits 1.
3. Plan from the commit the stack was last built from, kept in <state dir>/built (systemd's
   StateDirectory, /var/lib/brt-code-sync). The first sync records the HEAD it found before pulling.
   The record moves to the new HEAD only after a good rebuild, or when nothing between them needs one,
   so a rebuild that was postponed or failed is planned again, and alerted again, at every 17:30 until
   it goes through. A recorded commit that git does not know plans a rebuild that recreates every
   container. The files changed between the record and HEAD decide what runs:
   - a change to the stack's definition or the Airflow image's inputs (docker-compose.yaml,
     airflow.Dockerfile and its .dockerignore, every file the Dockerfile copies, any requirements
     file) needs a rebuild. A rebuild that changes nothing is cheap (the build is cached, and compose
     keeps every container whose image and settings are the same), while a missed one leaves new code
     on an old image;
   - .dvcignore is a single-file mount (ADR-023): a container keeps the file it started with until it
     is recreated, so a change to it recreates the containers;
   - anything else needs nothing: the directory mount shows it at once.
4. Rebuild in two steps, so no run is killed. `docker compose build` runs first, up to 30 minutes
   after a requirements change, while the old containers keep running. Then the run check again, and
   `docker compose up -d --wait` (with --force-recreate for .dvcignore), which takes seconds from the
   built image. A run that started during the build postpones the recreate to the next 17:30. .env
   must exist and not be empty, since without it compose starts on its development defaults (ADR-034).
   A good rebuild is followed by `docker image prune -f`, since old images would fill the 30 GB disk.
5. Alerts go to the journal, and to Slack when the repo's .env has SLACK_ALERT_WEBHOOK (the host has
   no such environment variable; startup.sh renders it from Secret Manager). The webhook URL is never
   printed.

    sudo systemctl start brt-code-sync.service     # run it now
    sudo journalctl -u brt-code-sync.service --no-pager
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import alerts

REPO = Path("/opt/boston-resilience-twin")  # startup.sh's CLONE_DIR
STATE_DIR = Path("/var/lib/brt-code-sync")  # StateDirectory=brt-code-sync in the service unit
BUILT_FILE = "built"  # the commit the stack was last built from
BRANCH = "main"
AIRFLOW_USER = "airflow"  # owns the checkout (#60)
DAG_ID = "daily_pipeline"
DOCKERFILE = "airflow.Dockerfile"
# The stack's definition and the Airflow image's build files (ADR-022). image_inputs adds the files
# the Dockerfile copies.
STACK_FILES = ("docker-compose.yaml", DOCKERFILE, f"{DOCKERFILE}.dockerignore")
FILE_MOUNTS = (".dvcignore",)  # single-file bind mounts in docker-compose.yaml
GIT_TIMEOUT_SECONDS = 120
CHECK_TIMEOUT_SECONDS = 120
# A build after a requirements change reinstalls packages. 30 minutes fits between the timer (17:30)
# and the evening run (18:05).
BUILD_TIMEOUT_SECONDS = 30 * 60
# Recreating from a built image takes seconds; --wait then waits for the health checks.
UP_TIMEOUT_SECONDS = 5 * 60
COMMIT = re.compile(r"[0-9a-f]{40}")

NONE, REBUILD, RECREATE = "none", "rebuild", "recreate"

Runner = Callable[..., subprocess.CompletedProcess]

log = logging.getLogger(__name__)


class SyncFailed(RuntimeError):
    """Something an operator must look at. The message is the alert."""


def copied_files(dockerfile_text: str) -> list[str]:
    """The build-context paths an image copies: the sources of each COPY and ADD, in shell or JSON
    form, without their --flags. A COPY --from=<stage> copies from another image, not from the repo,
    so it is left out."""
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


def check_branch(repo: Path, run: Runner) -> None:
    branch = git(repo, run, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise SyncFailed(f"the checkout is on {branch!r}, not {BRANCH}; the VM runs {BRANCH} only, so nothing was pulled")


def pull(repo: Path, run: Runner) -> tuple[str, str]:
    """Fast-forward the checkout to origin/main. Returns the old and the new HEAD."""
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
    """Run ids from `airflow dags list-runs ... -o json`, which prints "No data found" instead of an
    empty list, and may print warnings or log lines (which can start with `[` too) before the JSON."""
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


def scheduler_up(repo: Path, run: Runner) -> bool:
    """Whether the scheduler container is running. When it is not, no task is (LocalExecutor, ADR-016)."""
    result = call(run, ["docker", "compose", "ps", "--status", "running", "--quiet", "airflow-scheduler"],
                  cwd=repo, timeout=CHECK_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = last_line(result.stderr) or f"exit {result.returncode}"
        raise SyncFailed(f"could not tell whether the stack is up ({reason}); the sync waits for the next 17:30")
    return bool(result.stdout.strip())


def running_runs(repo: Path, run: Runner) -> list[str]:
    """The daily_pipeline runs in progress; none when the scheduler is not up."""
    if not scheduler_up(repo, run):
        return []
    args = ["docker", "compose", "exec", "-T", "airflow-scheduler",
            "airflow", "dags", "list-runs", DAG_ID, "--state", "running", "-o", "json"]
    result = call(run, args, cwd=repo, timeout=CHECK_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = last_line(result.stderr) or f"exit {result.returncode}"
        raise SyncFailed(f"could not check for a running {DAG_ID} run ({reason}); the sync waits for the next 17:30")
    return parse_runs(result.stdout)


def wait_for_runs(repo: Path, run: Runner, what: str) -> None:
    runs = running_runs(repo, run)
    if runs:
        raise SyncFailed(f"{DAG_ID} run {', '.join(runs)} is in progress, so {what} waits for the next 17:30")


def read_built(state_dir: Path) -> str | None:
    """The commit the stack was last built from, or None when there is no readable record."""
    path = state_dir / BUILT_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("cannot read %s (%s); starting a new record", path, e)
        return None
    if not COMMIT.fullmatch(text):
        log.warning("%s does not hold a commit (%r); starting a new record", path, text[:60])
        return None
    return text


def write_built(state_dir: Path, commit: str) -> None:
    """Record the commit the stack is built from, whole (a temp file renamed over the record)."""
    path = state_dir / BUILT_FILE
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(BUILT_FILE + ".tmp")
        temp.write_text(commit + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError as e:
        # Not fatal: the next sync plans from the older record, so at worst it rebuilds (cached) again.
        log.warning("cannot write %s (%s)", path, e)


def plan_from(repo: Path, run: Runner, built: str, new: str) -> tuple[list[str], str]:
    """The files changed since the recorded build, and what they need. A record git does not know
    (a rewritten history) plans a rebuild that recreates every container."""
    try:
        changed = git(repo, run, "diff", "--name-only", built, new).splitlines()
    except SyncFailed as e:
        log.warning("cannot diff from the recorded build %s (%s); rebuilding and recreating everything", built[:7], e)
        return [], RECREATE
    return changed, plan(changed, image_inputs(repo))


def rebuild(repo: Path, run: Runner, mode: str) -> None:
    """Build while the old containers run, check for a run again, then recreate in seconds."""
    if not env_ready(repo):
        raise SyncFailed(".env is missing or empty, and compose would start on its development defaults; "
                         "the rebuild waits (rerun the startup script first)")
    build = call(run, ["docker", "compose", "build"], cwd=repo, timeout=BUILD_TIMEOUT_SECONDS)
    if build.returncode != 0:
        reason = last_line(build.stderr) or f"exit {build.returncode}"
        raise SyncFailed(f"`docker compose build` failed ({reason}); the stack keeps running the old image, "
                         "and the next 17:30 tries again")
    wait_for_runs(repo, run, "the recreate (the image is built)")
    args = ["docker", "compose", "up", "-d", "--wait", *(["--force-recreate"] if mode == RECREATE else [])]
    result = call(run, args, cwd=repo, timeout=UP_TIMEOUT_SECONDS)
    if result.returncode != 0:
        reason = last_line(result.stderr) or f"exit {result.returncode}"
        raise SyncFailed(f"`{shlex.join(args)}` failed ({reason}); check `sudo docker compose ps`, "
                         "and the next 17:30 tries again")
    pruned = call(run, ["docker", "image", "prune", "-f"], cwd=repo, timeout=CHECK_TIMEOUT_SECONDS)
    if pruned.returncode != 0:
        log.warning("docker image prune failed: %s", last_line(pruned.stderr) or f"exit {pruned.returncode}")


def webhook_from_env(repo: Path) -> str | None:
    """SLACK_ALERT_WEBHOOK from the repo's .env, whose lines are VAR='value' (startup.sh), or None
    when it is absent, empty or not a Slack incoming webhook."""
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


def sync(repo: Path = REPO, run: Runner | None = None, post: Callable[[str, str], None] | None = None,
         state_dir: Path = STATE_DIR) -> int:
    """Wait for runs, pull, plan from the last build and rebuild. Returns 0, or 1 after an alert."""
    run = run or subprocess.run
    try:
        check_branch(repo, run)
        wait_for_runs(repo, run, "the pull")
        old, new = pull(repo, run)
        built = read_built(state_dir)
        if built is None:
            built = old  # the first sync: the stack runs what was checked out before this pull
            write_built(state_dir, built)
        if built == new:
            log.info("%s is up to date at %s, and the stack is built from it", BRANCH, new[:7])
            return 0
        changed, mode = plan_from(repo, run, built, new)
        pending = f" (a rebuild since {built[:7]} is still due)" if built != old else ""
        log.info("pulled %s..%s; %d files changed since the build at %s: %s%s",
                 old[:7], new[:7], len(changed), built[:7], mode, pending)
        if mode != NONE:
            rebuild(repo, run, mode)
            log.info("stack rebuilt (%s) at %s", mode, new[:7])
        write_built(state_dir, new)
        return 0
    except SyncFailed as e:
        alert(repo, str(e), post)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull main on airflow-vm and rebuild the Airflow stack when its inputs changed.")
    parser.add_argument("--repo", type=Path, default=REPO, help="the checkout (default: %(default)s)")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR,
                        help="where the commit of the last build is kept (default: %(default)s)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="brt-code-sync: %(levelname)s %(message)s")
    return sync(args.repo, state_dir=args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
