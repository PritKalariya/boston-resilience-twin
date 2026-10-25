import logging
import subprocess
import sys

import pytest

import dvc_push
from dvc_push import REPO_ROOT, has_google_credentials, is_no_access, main, push, remote_url

ANONYMOUS = (
    "ERROR: unexpected error - Anonymous caller does not have storage.objects.get access to the "
    "Google Cloud Storage object. Permission 'storage.objects.get' denied on resource "
    "'//storage.googleapis.com/projects/_/buckets/boston-resilience-twin-data/objects/dvc/files/md5/b1/946a', 401"
)
DENIED = (
    "ERROR: failed to push data to the cloud - grader@example.org does not have "
    "storage.objects.create access to the Google Cloud Storage object., 403"
)


def fake_run(returncode, stdout="", stderr=""):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    run.calls = calls
    return run


def test_no_credentials_skips_without_running_dvc(caplog, tmp_path):
    run = fake_run(0)
    with caplog.at_level(logging.WARNING):
        assert push(tmp_path, run=run, credentials=lambda: False) is False
    assert run.calls == []
    assert "dvc push skipped: no Google credentials" in caplog.text


def test_pushes_with_credentials(tmp_path):
    run = fake_run(0, stdout="3 files pushed\n")
    assert push(tmp_path, run=run, credentials=lambda: True) is True
    args, kwargs = run.calls[0]
    assert args == [sys.executable, "-m", "dvc", "push"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == dvc_push.PUSH_TIMEOUT_SECONDS


def test_hung_push_fails_with_the_timeout(tmp_path):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(RuntimeError, match="did not finish in 30 minutes"):
        push(tmp_path, run=run, credentials=lambda: True)


def test_log_shows_dvc_summary_not_stderr_warning(caplog, tmp_path):
    run = fake_run(0, stdout="1 file pushed\n", stderr="UserWarning: ... without a quota project\n")
    with caplog.at_level(logging.INFO):
        push(tmp_path, run=run, credentials=lambda: True)
    assert "1 file pushed" in caplog.text
    assert "quota project" not in caplog.text


def test_nothing_to_push_succeeds(tmp_path):
    assert push(tmp_path, run=fake_run(0, stdout="Everything is up to date.\n"), credentials=lambda: True) is True


@pytest.mark.parametrize("error", [ANONYMOUS, DENIED])
def test_refused_credentials_skip(caplog, tmp_path, error):
    with caplog.at_level(logging.WARNING):
        assert push(tmp_path, run=fake_run(255, stderr=error), credentials=lambda: True) is False
    assert "dvc push skipped: these credentials have no access" in caplog.text


def test_skip_logs_the_dvc_error_line(caplog, tmp_path):
    stderr = "UserWarning: ... without a quota project\n" + DENIED + "\n"
    with caplog.at_level(logging.WARNING):
        push(tmp_path, run=fake_run(255, stderr=stderr), credentials=lambda: True)
    assert f"DVC said: {DENIED}" in caplog.text
    assert "quota project" not in caplog.text


def test_other_failure_raises(tmp_path):
    run = fake_run(255, stderr="ERROR: failed to push data to the cloud - [Errno 28] No space left on device")
    with pytest.raises(RuntimeError, match="No space left"):
        push(tmp_path, run=run, credentials=lambda: True)


def test_empty_output_failure_raises(tmp_path):
    with pytest.raises(RuntimeError, match="exit 1"):
        push(tmp_path, run=fake_run(1), credentials=lambda: True)


@pytest.mark.parametrize(
    "text, expected",
    [(ANONYMOUS, True), (DENIED, True), ("ERROR: Connection reset by peer", False), ("", False)],
)
def test_is_no_access(text, expected):
    assert is_no_access(text) is expected


def test_remote_url_comes_from_the_repo_config():
    assert remote_url(REPO_ROOT) == "gs://boston-resilience-twin-data/dvc"


def test_remote_url_without_config(tmp_path):
    assert remote_url(tmp_path) == "the DVC remote"


def test_log_names_the_configured_remote(caplog, tmp_path):
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text("[core]\n    remote = other\n['remote \"other\"']\n    url = gs://elsewhere/dvc\n")
    with caplog.at_level(logging.INFO):
        push(tmp_path, run=fake_run(0, stdout="1 file pushed\n"), credentials=lambda: True)
    assert "dvc push to gs://elsewhere/dvc: 1 file pushed" in caplog.text


def test_has_google_credentials_false_without_any(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials():
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    assert has_google_credentials() is False


def test_has_google_credentials_true_and_quiet(monkeypatch, recwarn):
    import google.auth

    def with_warning():
        import warnings

        warnings.warn("Your application has authenticated using end user credentials without a quota project", UserWarning)
        return object(), None

    monkeypatch.setattr(google.auth, "default", with_warning)
    assert has_google_credentials() is True
    assert not [w for w in recwarn if "quota project" in str(w.message)]


def test_script_exits_0_without_credentials(tmp_path):
    """Criterion 3 end to end: the script, run as the pipeline runs it, with no
    Google credentials anywhere, skips with a log line and exits 0."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLOUDSDK_CONFIG": str(tmp_path / "gcloud"),  # no gcloud login
        "NO_GCE_CHECK": "true",  # no metadata server lookup; google-auth reads only lowercase "true"
    }
    result = subprocess.run(
        [sys.executable, dvc_push.__file__, "--repo", str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "dvc push skipped: no Google credentials" in result.stderr


def test_main_fails_on_other_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(dvc_push, "has_google_credentials", lambda: True)
    monkeypatch.setattr(dvc_push.subprocess, "run", fake_run(1, stderr="ERROR: boom"))
    with pytest.raises(RuntimeError, match="boom"):
        main(["--repo", str(tmp_path)])
