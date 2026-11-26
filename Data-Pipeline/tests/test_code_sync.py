import ast
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import alerts
import code_sync
from code_sync import NONE, REBUILD, RECREATE, SyncFailed, copied_files, image_inputs, parse_runs, plan, sync

OLD, NEW = "a" * 40, "b" * 40
WEBHOOK = "https://hooks.slack.com/services/T000/B000/xyz"
AS_AIRFLOW = ["runuser", "-u", "airflow", "--", "env", "HOME=/nonexistent"]
# The one COPY line of the real airflow.Dockerfile, which the test service does not mount.
REAL_COPY = "COPY --chown=airflow:0 Data-Pipeline/requirements.txt /opt/airflow/requirements.txt\n"


def done(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class Host:
    """A scripted VM host: answers each command code_sync runs and records it."""

    def __init__(self, *, branch="main", new=NEW, pull_error="", changed=(), runs="No data found",
                 runs_error="", up_error="", up_timeout=False):
        self.branch, self.head, self.new = branch, OLD, new
        self.pull_error, self.changed = pull_error, changed
        self.runs, self.runs_error = runs, runs_error
        self.up_error, self.up_timeout = up_error, up_timeout
        self.calls = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, kwargs))
        if "git" in args:
            assert args[:6] == AS_AIRFLOW, args  # git always runs as the checkout's owner
            sub = args[args.index("git") + 3:]  # after `git -C <repo>`
            if sub == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return done(args, stdout=f"{self.branch}\n")
            if sub == ["rev-parse", "HEAD"]:
                return done(args, stdout=f"{self.head}\n")
            if sub == ["pull", "--ff-only", "--quiet", "origin", "main"]:
                if self.pull_error:
                    return done(args, 1, stderr=self.pull_error)
                self.head = self.new
                return done(args)
            if sub == ["diff", "--name-only", OLD, NEW]:
                return done(args, stdout="".join(f"{path}\n" for path in self.changed))
        if args[:3] == ["docker", "compose", "exec"]:
            return done(args, 1 if self.runs_error else 0, stdout=self.runs, stderr=self.runs_error)
        if args[:3] == ["docker", "compose", "up"]:
            if self.up_timeout:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return done(args, 1 if self.up_error else 0, stderr=self.up_error)
        if args[:3] == ["docker", "image", "prune"]:
            return done(args, stdout="Total reclaimed space: 0B\n")
        raise AssertionError(f"unexpected command {args}")

    def docker(self):
        return [args for args, _ in self.calls if args[0] == "docker"]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".env").write_text(f"AIRFLOW_UID=50000\nPOSTGRES_PASSWORD='secret123'\nSLACK_ALERT_WEBHOOK='{WEBHOOK}'\n")
    return tmp_path


def recorder():
    sent = []

    def post(url, message):
        sent.append((url, message))

    post.sent = sent
    return post


# ---- what the Dockerfile copies, and the plan ----

def test_copied_files_reads_copy_and_add_sources():
    text = (
        "# COPY in a comment is not an instruction\n"
        + REAL_COPY
        + 'COPY ["Data-Pipeline/constraints.txt", "/opt/airflow/"]\n'
        + "ADD ./config/settings.csv /opt/\n"
        + "copy a.txt b.txt \\\n    /opt/both/\n"
        + "COPY --from=builder /opt/venv /opt/venv\n"
    )
    assert copied_files(text) == [
        "Data-Pipeline/requirements.txt", "Data-Pipeline/constraints.txt", "config/settings.csv", "a.txt", "b.txt",
    ]


def test_image_inputs_read_the_pulled_dockerfile(tmp_path):
    assert image_inputs(tmp_path) == list(code_sync.STACK_FILES)  # no Dockerfile: the fixed files only
    (tmp_path / "airflow.Dockerfile").write_text("FROM apache/airflow:3.3.2-python3.12\n" + REAL_COPY)
    assert image_inputs(tmp_path) == [*code_sync.STACK_FILES, "Data-Pipeline/requirements.txt"]


@pytest.mark.parametrize("changed, expected", [
    ([], NONE),
    (["Data-Pipeline/scripts/alerts.py", "Data-Pipeline/dags/daily_pipeline.py", "docs/runbook.md"], NONE),
    (["docker-compose.yaml"], REBUILD),
    (["airflow.Dockerfile"], REBUILD),
    (["airflow.Dockerfile.dockerignore"], REBUILD),
    (["Data-Pipeline/requirements.txt"], REBUILD),
    (["Model-Development/requirements.txt"], REBUILD),  # "a requirements file" (#162); a no-op rebuild is cheap
    ([".dvcignore"], RECREATE),
    (["docker-compose.yaml", ".dvcignore"], RECREATE),  # the recreate also builds
    (["Data-Pipeline/.dvcignore"], NONE),  # only the repo root's file is mounted on its own
])
def test_plan(changed, expected):
    assert plan(changed) == expected


def test_plan_follows_what_the_dockerfile_copies():
    inputs = [*code_sync.STACK_FILES, "Data-Pipeline/constraints.txt", "config/", "scripts/*.sh"]
    assert plan(["Data-Pipeline/constraints.txt"], inputs) == REBUILD
    assert plan(["config/settings.csv"], inputs) == REBUILD  # a copied directory
    assert plan(["scripts/entry.sh"], inputs) == REBUILD  # a copied glob
    assert plan(["configs/other.csv"], inputs) == NONE
    assert plan(["anything/at/all.py"], [*code_sync.STACK_FILES, "."]) == REBUILD  # COPY . copies everything


# ---- sync ----

def test_up_to_date_runs_nothing_else(repo, caplog):
    host = Host(new=OLD)
    with caplog.at_level(logging.INFO):
        assert sync(repo, run=host, post=recorder()) == 0
    assert host.docker() == []
    assert "main is up to date at aaaaaaa" in caplog.text


def test_git_pulls_main_fast_forward_only_as_the_checkout_owner(repo):
    host = Host(changed=["README.md"])
    assert sync(repo, run=host, post=recorder()) == 0
    pull = next(args for args, _ in host.calls if "pull" in args)
    assert pull == [*AS_AIRFLOW, "git", "-C", str(repo), "pull", "--ff-only", "--quiet", "origin", "main"]


def test_a_code_only_change_needs_no_rebuild(repo, caplog):
    host = Host(changed=["Data-Pipeline/scripts/alerts.py", "Data-Pipeline/dags/daily_pipeline.py"])
    post = recorder()
    with caplog.at_level(logging.INFO):
        assert sync(repo, run=host, post=post) == 0
    assert host.docker() == []
    assert post.sent == []
    assert "pulled aaaaaaa..bbbbbbb: 2 files changed, none" in caplog.text


def test_an_image_input_change_rebuilds_from_the_repo(repo):
    host = Host(changed=["Data-Pipeline/requirements.txt"])
    assert sync(repo, run=host, post=recorder()) == 0
    assert host.docker() == [
        ["docker", "compose", "exec", "-T", "airflow-scheduler",
         "airflow", "dags", "list-runs", "daily_pipeline", "--state", "running", "-o", "json"],
        ["docker", "compose", "up", "-d", "--build", "--wait"],
        ["docker", "image", "prune", "-f"],
    ]
    assert all(kwargs["cwd"] == repo for args, kwargs in host.calls if args[0] == "docker")
    up = next(kwargs for args, kwargs in host.calls if args[:3] == ["docker", "compose", "up"])
    assert up["timeout"] == code_sync.BUILD_TIMEOUT_SECONDS


def test_a_dvcignore_change_recreates_the_containers(repo):
    host = Host(changed=[".dvcignore"])
    assert sync(repo, run=host, post=recorder()) == 0
    assert ["docker", "compose", "up", "-d", "--build", "--wait", "--force-recreate"] in host.docker()


def test_a_failed_pull_alerts_and_touches_nothing(repo):
    host = Host(pull_error="fatal: Not possible to fast-forward, aborting.\n")
    post = recorder()
    assert sync(repo, run=host, post=post) == 1
    assert host.docker() == []
    assert not any("diff" in args for args, _ in host.calls)
    [(url, message)] = post.sent
    assert url == WEBHOOK
    assert message == ("brt-code-sync on airflow-vm: git pull --ff-only failed "
                       "(fatal: Not possible to fast-forward, aborting.); the checkout is still at aaaaaaa")


def test_another_branch_is_not_pulled(repo):
    host = Host(branch="162-try-on-the-vm")
    post = recorder()
    assert sync(repo, run=host, post=post) == 1
    assert not any("pull" in args for args, _ in host.calls)
    assert "the checkout is on '162-try-on-the-vm', not main" in post.sent[0][1]


@pytest.mark.parametrize("env", [None, ""])
def test_no_env_file_postpones_the_rebuild(repo, env):
    if env is None:
        (repo / ".env").unlink()
    else:
        (repo / ".env").write_text(env)
    host = Host(changed=["docker-compose.yaml"])
    assert sync(repo, run=host, post=recorder()) == 1
    assert host.docker() == []  # not even the run check


def test_a_running_dag_run_postpones_the_rebuild(repo):
    runs = json.dumps([{"dag_id": "daily_pipeline", "run_id": "manual__2026-11-24T17:20:00+00:00", "state": "running"}])
    host = Host(changed=["docker-compose.yaml"], runs=runs)
    post = recorder()
    assert sync(repo, run=host, post=post) == 1
    assert not any(args[:3] == ["docker", "compose", "up"] for args in host.docker())
    assert "daily_pipeline run manual__2026-11-24T17:20:00+00:00 is in progress" in post.sent[0][1]


def test_an_unknown_run_state_postpones_the_rebuild(repo):
    host = Host(changed=["docker-compose.yaml"], runs_error='service "airflow-scheduler" is not running\n')
    post = recorder()
    assert sync(repo, run=host, post=post) == 1
    assert not any(args[:3] == ["docker", "compose", "up"] for args in host.docker())
    assert 'could not check for a running daily_pipeline run (service "airflow-scheduler" is not running)' in post.sent[0][1]


def test_a_failed_build_alerts_with_dockers_last_line(repo):
    host = Host(changed=["Data-Pipeline/requirements.txt"],
                up_error="#7 resolving\nERROR: failed to solve: process did not complete successfully: exit code: 1\n")
    post = recorder()
    assert sync(repo, run=host, post=post) == 1
    assert ["docker", "image", "prune", "-f"] not in host.docker()
    assert "(ERROR: failed to solve: process did not complete successfully: exit code: 1)" in post.sent[0][1]
    assert "the stack may still run the old image" in post.sent[0][1]


def test_a_hung_build_is_an_alert(repo):
    host = Host(changed=["airflow.Dockerfile"], up_timeout=True)
    post = recorder()
    assert sync(repo, run=host, post=post) == 1
    assert "`docker compose up -d --build --wait` did not finish in 30 minutes" in post.sent[0][1]


def test_a_missing_command_is_an_alert(repo):
    def no_runuser(args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    post = recorder()
    assert sync(repo, run=no_runuser, post=post) == 1
    assert "could not run `git -C" in post.sent[0][1]


# ---- the alert channel ----

@pytest.mark.parametrize("line, expected", [
    (f"SLACK_ALERT_WEBHOOK='{WEBHOOK}'", WEBHOOK),
    (f"SLACK_ALERT_WEBHOOK={WEBHOOK}", WEBHOOK),
    ("SLACK_ALERT_WEBHOOK=''", None),  # an optional secret with no version (ADR-034)
    ("SLACK_ALERT_WEBHOOK='https://example.org/hook'", None),  # not a Slack webhook
    ("OTHER='x'", None),
])
def test_the_webhook_comes_from_the_env_file(tmp_path, line, expected):
    (tmp_path / ".env").write_text(f"AIRFLOW_UID=50000\n{line}\n")
    assert code_sync.webhook_from_env(tmp_path) == expected


def test_without_a_webhook_the_alert_is_in_the_journal_only(repo, caplog):
    (repo / ".env").write_text("AIRFLOW_UID=50000\nSLACK_ALERT_WEBHOOK=''\n")
    post = recorder()
    with caplog.at_level(logging.WARNING):
        assert sync(repo, run=Host(pull_error="fatal: unable to access GitHub\n"), post=post) == 1
    assert post.sent == []
    assert "ALERT git pull --ff-only failed (fatal: unable to access GitHub)" in caplog.text
    assert "the alert is in the journal only" in caplog.text


def test_the_webhook_is_never_logged(repo, caplog):
    with caplog.at_level(logging.DEBUG):
        sync(repo, run=Host(pull_error="fatal: boom\n"), post=recorder())
    assert "hooks.slack.com" not in caplog.text


def test_a_refused_alert_is_logged_not_raised(repo, caplog):
    def refused(url, message):
        raise alerts.AlertNotSent("Slack refused the alert: HTTP 404 Not Found")

    with caplog.at_level(logging.ERROR):
        assert sync(repo, run=Host(pull_error="fatal: boom\n"), post=refused) == 1
    assert "could not send the alert: Slack refused the alert: HTTP 404" in caplog.text


def test_alerts_use_the_pipeline_slack_sender_by_default(repo, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "post_to_slack", lambda url, message: sent.append(message))
    assert sync(repo, run=Host(pull_error="fatal: boom\n")) == 1
    assert sent == ["brt-code-sync on airflow-vm: git pull --ff-only failed (fatal: boom); the checkout is still at aaaaaaa"]


# ---- list-runs output, main, and the host's Python ----

def test_parse_runs():
    assert parse_runs("No data found\n") == []
    assert parse_runs("") == []
    runs = json.dumps([{"run_id": "scheduled__x"}, {"run_id": "manual__y"}], indent=4)
    assert parse_runs("UserWarning: something\n" + runs) == ["scheduled__x", "manual__y"]
    log_line = "[2026-11-24T22:30:01.123+0000] {dagbag.py:588} INFO - Filling up the DagBag\n"
    assert parse_runs(log_line + runs) == ["scheduled__x", "manual__y"]
    with pytest.raises(SyncFailed, match="could not read the running daily_pipeline runs"):
        parse_runs("Traceback (most recent call last):")


def test_main_returns_the_sync_exit_code(repo, monkeypatch):
    monkeypatch.setattr(code_sync.subprocess, "run", Host(pull_error="fatal: boom\n"))
    monkeypatch.setattr(alerts, "post_to_slack", lambda url, message: None)
    assert code_sync.main(["--repo", str(repo)]) == 1


def test_the_host_needs_only_the_standard_library():
    # The service runs the host's python3, which has no pip packages.
    scripts = Path(code_sync.__file__).resolve().parent
    allowed = set(sys.stdlib_module_names) | {"__future__", "alerts", "credentials"}
    for name in ("code_sync", "alerts", "credentials"):
        tree = ast.parse((scripts / f"{name}.py").read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert imported <= allowed, (name, imported - allowed)
