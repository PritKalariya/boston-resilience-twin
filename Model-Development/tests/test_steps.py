"""Tests for the model workflow's stub steps and the Slack notifier (#112, ADR-027).

Steps run in process through their main(); the notify tests replace the network post.
Nothing here reaches a network.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "Model-Development" / "scripts"
STEPS = SCRIPTS / "steps"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pipeline"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(STEPS))
import _common as C  # noqa: E402
import evaluate  # noqa: E402
import gates as G  # noqa: E402
import notify  # noqa: E402
import pull_data  # noqa: E402
import registry_push  # noqa: E402
import reload_check  # noqa: E402
import rescore_champion  # noqa: E402
import set_champion  # noqa: E402
import train  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("STUB_OUTCOME", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)


def args(art: Path) -> list[str]:
    return ["--artifacts", str(art)]


def read(path: Path):
    return json.loads(path.read_text())


def run_to_gates(art: Path, outcome: str = "pass", main_only: bool = False) -> int:
    """The chain up to and including the gates, as the workflow runs it. Returns the gates exit."""
    for step in (pull_data, train, evaluate):
        assert step.main(args(art)) == 0
    if main_only:
        assert rescore_champion.main(args(art)) == 0
    return G.main(["--metrics", str(art / "metrics.json"), "--out", str(art / "gate_report.json")])


# --- pull_data -------------------------------------------------------------


def test_pull_data_uses_local_without_sha(tmp_path):
    assert pull_data.main(args(tmp_path)) == 0
    assert (tmp_path / "data_version.txt").read_text().strip() == "stub-local"


def test_pull_data_uses_github_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    assert pull_data.main(args(tmp_path)) == 0
    assert (tmp_path / "data_version.txt").read_text().strip() == "stub-abc123"


def test_bad_stub_outcome_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_OUTCOME", "nonsense")
    assert pull_data.main(args(tmp_path)) == 2


def test_artifacts_flag_is_required(tmp_path):
    with pytest.raises(SystemExit) as exc:
        pull_data.main([])
    assert exc.value.code == 2


# --- train -----------------------------------------------------------------


def test_train_writes_model_and_scores(tmp_path):
    pull_data.main(args(tmp_path))
    assert train.main(args(tmp_path)) == 0
    model = read(tmp_path / "model" / "model.json")
    assert model["type"] == "constant"
    row = read(C.FIXTURE_ROW)
    assert read(tmp_path / "train_scores.json") == {"fixture_row_score": C.score(model, row)}


def test_train_needs_data_version(tmp_path):
    assert train.main(args(tmp_path)) == 2


# --- evaluate and gates ----------------------------------------------------


def test_evaluate_needs_a_model(tmp_path):
    pull_data.main(args(tmp_path))
    assert evaluate.main(args(tmp_path)) == 2


def test_pass_chain_promotes(tmp_path):
    assert run_to_gates(tmp_path) == 0
    report = read(tmp_path / "gate_report.json")
    assert report["decision"] == "promote"
    assert report["data_version"] == "stub-local"
    assert report["fairness_gate"]["gap"] == 0


def test_metrics_counts_reconcile(tmp_path):
    run_to_gates(tmp_path)
    m = read(tmp_path / "metrics.json")
    positives = m["candidate"]["tp"] + m["candidate"]["fn"]
    assert m["baseline"]["tp"] + m["baseline"]["fn"] == positives
    parts = list(m["groups"].values()) + list(m["excluded"].values())
    assert sum(p["positives"] for p in parts) == positives
    assert sum(p["true_positives"] for p in parts) == m["candidate"]["tp"]
    assert m["champion"] is None


def test_fail_gate_blocks_with_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_OUTCOME", "fail_gate")
    assert run_to_gates(tmp_path) == 1
    report = read(tmp_path / "gate_report.json")
    assert report["decision"] == "blocked"
    assert report["fairness_gate"]["gap"] > 0.15
    assert report["f1_gate"]["pass"] is True


def test_fail_pipeline_passes_the_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_OUTCOME", "fail_pipeline")
    assert run_to_gates(tmp_path) == 0


# --- main-only steps -------------------------------------------------------


def test_rescore_champion_leaves_champion_null(tmp_path):
    run_to_gates(tmp_path, main_only=True)
    assert read(tmp_path / "metrics.json")["champion"] is None
    assert read(tmp_path / "champion_rescore.json")["champion"] is None


def test_rescore_champion_needs_metrics(tmp_path):
    assert rescore_champion.main(args(tmp_path)) == 2


def test_rescore_champion_rejects_non_object_metrics(tmp_path):
    (tmp_path / "metrics.json").write_text("[1]")
    assert rescore_champion.main(args(tmp_path)) == 2


def test_reload_check_ok(tmp_path, capsys):
    run_to_gates(tmp_path)
    assert reload_check.main(args(tmp_path)) == 0
    assert "reload check ok" in capsys.readouterr().out


def test_reload_check_fail_pipeline_exits_2(tmp_path, monkeypatch, capsys):
    run_to_gates(tmp_path)
    monkeypatch.setenv("STUB_OUTCOME", "fail_pipeline")
    assert reload_check.main(args(tmp_path)) == 2
    assert "reload mismatch" in capsys.readouterr().err


def test_reload_check_catches_a_changed_model(tmp_path):
    run_to_gates(tmp_path)
    path = tmp_path / "model" / "model.json"
    model = read(path)
    model["score"] += 1e-6
    path.write_text(json.dumps(model))
    assert reload_check.main(args(tmp_path)) == 2


def test_reload_check_tolerates_float_noise(tmp_path):
    run_to_gates(tmp_path)
    path = tmp_path / "model" / "model.json"
    model = read(path)
    model["score"] += 1e-12
    path.write_text(json.dumps(model))
    assert reload_check.main(args(tmp_path)) == 0


def test_reload_check_needs_train_scores(tmp_path):
    run_to_gates(tmp_path)
    (tmp_path / "train_scores.json").write_text("{}")
    assert reload_check.main(args(tmp_path)) == 2


def test_registry_push_writes_stub_version(tmp_path):
    run_to_gates(tmp_path)
    assert registry_push.main(args(tmp_path)) == 0
    registry = read(tmp_path / "registry.json")
    assert registry["model_version"] == "stub-model-stub-local"
    assert registry["pushed"] is False
    assert registry["gates_sha256"] == read(tmp_path / "gate_report.json")["gates_sha256"]


def test_registry_push_needs_a_gate_report(tmp_path):
    run_to_gates(tmp_path)
    (tmp_path / "gate_report.json").unlink()
    assert registry_push.main(args(tmp_path)) == 2


def test_registry_push_rejects_non_object_report(tmp_path):
    run_to_gates(tmp_path)
    (tmp_path / "gate_report.json").write_text("[]")
    assert registry_push.main(args(tmp_path)) == 2


def test_set_champion_promote_writes_would_set(tmp_path, capsys):
    run_to_gates(tmp_path)
    registry_push.main(args(tmp_path))
    assert set_champion.main(args(tmp_path)) == 0
    assert read(tmp_path / "champion.json")["action"] == "would set champion"
    assert "would set champion" in capsys.readouterr().out


def test_set_champion_keep_changes_nothing(tmp_path, capsys):
    run_to_gates(tmp_path)
    registry_push.main(args(tmp_path))
    report = read(tmp_path / "gate_report.json")
    report["decision"] = "keep_champion"
    (tmp_path / "gate_report.json").write_text(json.dumps(report))
    assert set_champion.main(args(tmp_path)) == 0
    assert not (tmp_path / "champion.json").exists()
    assert "keep champion" in capsys.readouterr().out


@pytest.mark.parametrize("decision", ["blocked", "error", None])
def test_set_champion_rejects_other_decisions(tmp_path, decision):
    (tmp_path / "gate_report.json").write_text(json.dumps({"decision": decision}))
    assert set_champion.main(args(tmp_path)) == 2


def test_set_champion_rejects_non_object_report(tmp_path):
    (tmp_path / "gate_report.json").write_text("[]")
    assert set_champion.main(args(tmp_path)) == 2


def test_common_rejects_a_bad_model_and_empty_version(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"type": "forest"}))
    with pytest.raises(C.StepError):
        C.load_model(tmp_path / "m.json")
    (tmp_path / "data_version.txt").write_text("  \n")
    with pytest.raises(C.StepError):
        C.read_data_version(tmp_path)


# --- notify ----------------------------------------------------------------

RUN = {"repo": "org/repo", "ref": "main", "run_url": "https://example.invalid/run/1"}


def notify_args(art: Path, status: str, gates_exit: str = "", main: bool = False) -> list[str]:
    out = [
        "--artifacts", str(art), "--repo", RUN["repo"], "--ref", RUN["ref"],
        "--run-url", RUN["run_url"], "--job-status", status, "--gates-exit", gates_exit,
    ]
    return out + (["--main"] if main else [])


@pytest.mark.parametrize(
    ("status", "gates_exit", "is_main", "expected"),
    [
        ("failure", "1", False, notify.GATE_FAILED),
        ("failure", "1", True, notify.GATE_FAILED),
        ("failure", "2", False, notify.PIPELINE_FAILED),
        ("failure", "", True, notify.PIPELINE_FAILED),
        ("success", "0", True, notify.TRAINING_COMPLETE),
        ("success", "0", False, None),
        ("success", "", False, None),
        ("cancelled", "", True, None),
        ("cancelled", "1", True, notify.GATE_FAILED),
    ],
)
def test_route(status, gates_exit, is_main, expected):
    assert notify.route(status, gates_exit, is_main) == expected


def test_message_carries_the_run_facts(tmp_path):
    run_to_gates(tmp_path)
    text = notify.build_message(notify.TRAINING_COMPLETE, tmp_path, **RUN)
    for expected in (
        "training complete", "org/repo", "main", "https://example.invalid/run/1",
        "stub-local", "Decision: promote", "F1 margin: 0.2222", "gap: 0.0000",
    ):
        assert expected in text


def test_message_without_artifacts_says_na(tmp_path):
    text = notify.build_message(notify.PIPELINE_FAILED, tmp_path, **RUN)
    assert "Data version: n/a" in text
    assert "Decision: n/a" in text
    assert "F1 margin: n/a" in text


def test_message_for_an_error_report(tmp_path):
    shutil.copy(FIXTURES / "gate_report_error.json", tmp_path / "gate_report.json")
    text = notify.build_message(notify.GATE_FAILED, tmp_path, **RUN)
    assert "Decision: error" in text
    assert "F1 margin: n/a" in text


def test_message_escapes_slack_characters(tmp_path):
    text = notify.build_message(notify.PIPELINE_FAILED, tmp_path, "a&b", "<x>", "u")
    assert "a&amp;b" in text
    assert "&lt;x&gt;" in text


def test_notify_without_webhook_warns_and_exits_0(tmp_path, capsys):
    run_to_gates(tmp_path)
    assert notify.main(notify_args(tmp_path, "success", "0", main=True)) == 0
    out = capsys.readouterr().out
    assert "::warning::SLACK_ALERT_WEBHOOK is empty" in out
    assert "Model pipeline: training complete" in out


def test_notify_posts_one_payload(tmp_path, monkeypatch):
    run_to_gates(tmp_path)
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append((url, payload)))
    assert notify.main(notify_args(tmp_path, "success", "0", main=True)) == 0
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "https://example.invalid/hook"
    assert list(payload) == ["text"]
    assert "training complete" in payload["text"]
    assert "example.invalid/hook" not in payload["text"]


def test_notify_pr_success_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda *a: pytest.fail("posted"))
    assert notify.main(notify_args(tmp_path, "success", "0")) == 0
    assert "no Slack message" in capsys.readouterr().out


def test_notify_gate_failure_on_a_pr(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append(payload))
    assert notify.main(notify_args(tmp_path, "failure", "1")) == 0
    assert "gate failed" in sent[0]["text"]


def test_notify_missing_artifacts_dir_still_sends_pipeline_failed(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append(payload))
    missing = tmp_path / "does-not-exist"
    assert notify.main(notify_args(missing, "failure", "", main=True)) == 0
    assert len(sent) == 1
    assert "pipeline failed" in sent[0]["text"]
    assert "Data version: n/a" in sent[0]["text"]
    assert not missing.exists()


def test_notify_empty_gates_exit_is_pipeline_failed(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append(payload))
    argv = notify_args(tmp_path, "failure", "")
    assert argv[argv.index("--gates-exit") + 1] == ""
    assert notify.main(argv) == 0
    assert "pipeline failed" in sent[0]["text"]
    # the flag left out altogether behaves the same
    sent.clear()
    without = [a for i, a in enumerate(argv) if a != "--gates-exit" and argv[i - 1] != "--gates-exit"]
    assert notify.main(without) == 0
    assert "pipeline failed" in sent[0]["text"]


def test_notify_undecodable_data_version_does_not_crash(tmp_path):
    (tmp_path / "data_version.txt").write_bytes(b"\xff\xfe")
    assert "Data version: n/a" in notify.build_message(notify.PIPELINE_FAILED, tmp_path, **RUN)


def test_notify_rejects_a_non_https_webhook(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(notify.WEBHOOK_ENV, "ftp://example.invalid/secret-token")
    monkeypatch.setattr(notify, "post", lambda *a: pytest.fail("posted"))
    assert notify.main(notify_args(tmp_path, "failure", "")) == 0
    out = capsys.readouterr().out
    assert "not an https URL" in out
    assert "secret-token" not in out


def test_notify_post_failure_is_a_warning_without_the_url(tmp_path, monkeypatch, capsys):
    def boom(url, payload):
        raise OSError(f"cannot reach {url}")

    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/secret-token")
    monkeypatch.setattr(notify, "post", boom)
    assert notify.main(notify_args(tmp_path, "failure", "")) == 0
    out = capsys.readouterr().out
    assert "::warning::Slack post failed (OSError)" in out
    assert "secret-token" not in out
