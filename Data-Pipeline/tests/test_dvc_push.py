import logging
import subprocess

import pytest

from dvc_push import is_no_access, push

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
    assert args == ["dvc", "push"]
    assert kwargs["cwd"] == tmp_path


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
