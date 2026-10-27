import json
import logging
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

import alerts


def failed_context(error=ValueError("population.csv is empty")):
    ti = SimpleNamespace(dag_id="daily_pipeline", task_id="ingest_static_layers", try_number=3)
    return {"ti": ti, "run_id": "slice-2025", "exception": error}


def test_alert_names_the_dag_task_run_try_and_error():
    assert alerts.alert_message(failed_context()) == (
        "daily_pipeline.ingest_static_layers failed in run slice-2025 (try 3): ValueError: population.csv is empty"
    )


def test_alert_falls_back_to_the_dag_run():
    context = {"dag_run": SimpleNamespace(dag_id="daily_pipeline", run_id="full-build"), "exception": RuntimeError("boom")}
    assert alerts.alert_message(context) == "daily_pipeline.unknown task failed in run full-build (try ?): RuntimeError: boom"


def test_alert_with_an_empty_context():
    assert alerts.alert_message({}) == (
        "unknown DAG.unknown task failed in run unknown run (try ?): "
        "no exception recorded (the task may have been killed; check the scheduler log)"
    )


def test_failure_callback_writes_the_alert_to_the_task_log(caplog):
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.on_task_failure(failed_context())
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.messages[-1] == (
        "ALERT daily_pipeline.ingest_static_layers failed in run slice-2025 (try 3): ValueError: population.csv is empty"
    )


def test_failure_callback_never_raises(monkeypatch, caplog):
    def broken(message):
        raise ConnectionError("Slack is down")

    monkeypatch.setattr(alerts, "send_alert", broken)
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.on_task_failure(failed_context())
    assert caplog.messages[-1] == "could not send the failure alert: Slack is down"


# Slack delivery (#34). A made-up webhook: tests never reach Slack.
WEBHOOK = "https://hooks.slack.com/services/T000/B000/not-a-real-secret"


def recorder():
    calls = []

    def post(url, message):
        calls.append((url, message))

    post.calls = calls
    return post


def test_without_a_webhook_the_alert_stays_in_the_task_log(monkeypatch, caplog):
    monkeypatch.delenv(alerts.SLACK_WEBHOOK_ENV, raising=False)
    post = recorder()
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.send_alert("daily_pipeline.validate_raw failed", post=post)
    assert post.calls == []
    assert caplog.messages[-1] == "ALERT daily_pipeline.validate_raw failed"


def test_with_a_webhook_the_alert_is_logged_and_posted_once(monkeypatch, caplog):
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)
    post = recorder()
    with caplog.at_level(logging.INFO, logger="alerts"):
        alerts.send_alert("daily_pipeline.validate_raw failed", post=post)
    assert post.calls == [(WEBHOOK, "daily_pipeline.validate_raw failed")]
    assert "ALERT daily_pipeline.validate_raw failed" in caplog.messages


class ValidationFailed(RuntimeError):
    """Stands in for validate_data.ValidationFailed (#17), which names each failing check."""


def test_one_failed_task_posts_one_alert_naming_the_failing_check(monkeypatch):
    error = ValidationFailed("1 raw checks failed: raw 2026.csv header: expect_column_to_exist(open_dt) observed missing")
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)
    post = recorder()
    monkeypatch.setattr(alerts, "post_to_slack", post)
    ti = SimpleNamespace(dag_id="daily_pipeline", task_id="validate_raw", try_number=1)
    alerts.on_task_failure({"ti": ti, "run_id": "bad-file-test", "exception": error})
    assert len(post.calls) == 1
    message = post.calls[0][1]
    assert message.startswith("daily_pipeline.validate_raw failed in run bad-file-test (try 1): ValidationFailed:")
    assert "expect_column_to_exist(open_dt)" in message


def test_slack_down_never_fails_the_run_or_logs_the_url(monkeypatch, caplog):
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)

    def down(url, message):
        raise alerts.AlertNotSent("Slack could not be reached: timed out")

    monkeypatch.setattr(alerts, "post_to_slack", down)
    with caplog.at_level(logging.INFO, logger="alerts"):
        alerts.on_task_failure(failed_context())
    assert caplog.messages[-1] == "could not send the failure alert: Slack could not be reached: timed out"
    assert "hooks.slack.com" not in caplog.text


def test_a_webhook_that_is_not_slack_is_refused(monkeypatch, caplog):
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, "https://example.org/collect")
    post = recorder()
    with caplog.at_level(logging.WARNING, logger="alerts"):
        alerts.send_alert("x", post=post)
    assert post.calls == []
    assert "is not a Slack incoming webhook" in caplog.text
    assert "example.org" not in caplog.text


def test_blank_webhook_counts_as_unset(monkeypatch):
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, "   ")
    assert alerts.slack_webhook() is None


def test_slack_text_escapes_control_characters_and_the_log_keeps_them(monkeypatch, caplog):
    monkeypatch.setenv("CENSUS_API_KEY", "abc123census456key")
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)
    post = recorder()
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.send_alert("key=abc123census456key; value <NA> & <class 'float'>", post=post)
    assert "key=<CENSUS_API_KEY>; value <NA> & <class 'float'>" in caplog.text  # raw in the log
    assert alerts.slack_text(post.calls[0][1]).endswith(
        "key=&lt;CENSUS_API_KEY&gt;; value &lt;NA&gt; &amp; &lt;class 'float'&gt;")


def test_ampersand_is_escaped_first():
    assert alerts.slack_escape("a &lt; b") == "a &amp;lt; b"


def test_a_cut_never_splits_an_entity():
    for pad in range(6):
        text = alerts.slack_text("x" * (alerts.SLACK_MAX_CHARS - 80 + pad) + "&" * 50)
        assert len(text) <= alerts.SLACK_MAX_CHARS
        body = text.split("\n... (cut;")[0]
        assert not body.endswith(("&", "&a", "&am", "&amp")), body[-6:]


def test_long_alerts_are_cut_for_slack():
    text = alerts.slack_text("x" * 10_000)
    assert len(text) <= alerts.SLACK_MAX_CHARS
    assert text.endswith("(cut; the full alert is in the task log)")


class SlackStub(BaseHTTPRequestHandler):
    """A local stand-in for Slack's webhook endpoint (loopback is allowed in tests)."""

    status = 200
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        SlackStub.received.append((self.path, self.headers["Content-Type"], json.loads(self.rfile.read(length))))
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok" if self.status == 200 else b"no_service")

    def log_message(self, *args):
        pass


def serve(status):
    SlackStub.status, SlackStub.received = status, []
    server = HTTPServer(("127.0.0.1", 0), SlackStub)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/services/T000/B000/xyz"


def test_post_to_slack_sends_the_webhook_json():
    server, url = serve(200)
    try:
        alerts.post_to_slack(url, "daily_pipeline.validate_raw failed", timeout=5)
    finally:
        server.server_close()
    path, content_type, body = SlackStub.received[0]
    assert path == "/services/T000/B000/xyz"
    assert content_type == "application/json"
    assert body == {"text": ":rotating_light: *Pipeline alert*\ndaily_pipeline.validate_raw failed"}


def test_post_to_slack_sends_escaped_text():
    server, url = serve(200)
    try:
        alerts.post_to_slack(url, "observed <NA> & more", timeout=5)
    finally:
        server.server_close()
    assert SlackStub.received[0][2]["text"].endswith("observed &lt;NA&gt; &amp; more")


def test_post_to_slack_raises_on_a_refused_webhook_without_the_url():
    server, url = serve(404)
    try:
        with pytest.raises(alerts.AlertNotSent, match="HTTP 404") as e:
            alerts.post_to_slack(url, "x", timeout=5)
    finally:
        server.server_close()
    assert "127.0.0.1" not in str(e.value) and "services" not in str(e.value)


def test_test_command_without_a_webhook_logs_only(monkeypatch, capsys, caplog):
    monkeypatch.delenv(alerts.SLACK_WEBHOOK_ENV, raising=False)
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.main(["--test"])
    assert "is not set" in capsys.readouterr().out
    assert caplog.messages[-1].startswith("ALERT test alert from alerts.py --test")


# Masking credentials (Piyush's review of #77). Made-up values, never real keys.
CENSUS_KEY = "abc123census456key"
NWS_AGENT = "(boston-resilience-twin, someone@example.org)"


def test_a_key_in_a_request_url_is_masked_in_the_log_and_slack(monkeypatch, caplog):
    monkeypatch.setenv("CENSUS_API_KEY", CENSUS_KEY)
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)
    post = recorder()
    error = f"HTTPError: 500 Server Error for url: https://api.census.gov/data/2023/acs/acs5?get=NAME&key={CENSUS_KEY}"
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.send_alert(f"daily_pipeline.ingest_acs failed: {error}", post=post)
    assert CENSUS_KEY not in caplog.text
    assert "key=<CENSUS_API_KEY>" in caplog.text
    assert CENSUS_KEY not in post.calls[0][1]
    assert "key=<CENSUS_API_KEY>" in post.calls[0][1]


def test_url_encoded_values_are_masked_too(monkeypatch):
    monkeypatch.setenv("NWS_USER_AGENT", NWS_AGENT)
    quoted = "%28boston-resilience-twin%2C%20someone%40example.org%29"
    plus = "%28boston-resilience-twin%2C+someone%40example.org%29"
    message = f"raw {NWS_AGENT} quoted {quoted} plus {plus}"
    assert alerts.mask_secrets(message) == "raw <NWS_USER_AGENT> quoted <NWS_USER_AGENT> plus <NWS_USER_AGENT>"


def test_the_webhook_itself_is_masked(monkeypatch):
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)
    assert alerts.mask_secrets(f"posting to {WEBHOOK} failed") == "posting to <SLACK_ALERT_WEBHOOK> failed"


def test_short_or_unset_values_are_left_alone(monkeypatch):
    monkeypatch.setenv("NOAA_CDO_TOKEN", "abc")
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    assert alerts.mask_secrets("abc failed in run abc") == "abc failed in run abc"


def test_a_failed_send_does_not_leak_a_key_either(monkeypatch, caplog):
    monkeypatch.setenv("NOAA_CDO_TOKEN", "noaa-token-0123456789")
    monkeypatch.setenv(alerts.SLACK_WEBHOOK_ENV, WEBHOOK)

    def down(url, message):
        raise RuntimeError("proxy said: bad header token noaa-token-0123456789")

    monkeypatch.setattr(alerts, "post_to_slack", down)
    with caplog.at_level(logging.ERROR, logger="alerts"):
        alerts.on_task_failure(failed_context())
    assert "noaa-token-0123456789" not in caplog.text
    assert caplog.messages[-1] == "could not send the failure alert: proxy said: bad header token <NOAA_CDO_TOKEN>"


def test_post_to_slack_raises_when_slack_cannot_be_reached(monkeypatch):
    def unreachable(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", unreachable)
    with pytest.raises(alerts.AlertNotSent, match="Slack could not be reached: connection refused"):
        alerts.post_to_slack("https://hooks.slack.com/services/T000/B000/xyz", "x")


def test_post_to_slack_raises_on_an_answer_other_than_200():
    server, url = serve(202)
    try:
        with pytest.raises(alerts.AlertNotSent, match="Slack answered HTTP 202"):
            alerts.post_to_slack(url, "x", timeout=5)
    finally:
        server.server_close()


def test_main_without_test_is_a_usage_error():
    with pytest.raises(SystemExit) as exited:
        alerts.main([])
    assert exited.value.code == 2
