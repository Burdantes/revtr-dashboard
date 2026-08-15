from datetime import datetime, timedelta, timezone

import pytest

import alerting


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Severity ladder
# ---------------------------------------------------------------------------


def test_no_reasons_is_ok():
    assert alerting.classify([]) == "ok"


def test_one_reason_is_warning():
    assert alerting.classify(["Failure spike: fail 0.30 vs baseline 0.10"]) == "warning"


def test_two_reasons_is_high():
    assert alerting.classify(["a", "b"]) == "high"


def test_three_reasons_is_critical():
    assert alerting.classify(["a", "b", "c"]) == "critical"


def test_hard_failure_is_critical_even_when_alone():
    """A total outage raises exactly one reason. It must still be critical.

    This is the ladder inversion the count-only rule had: 'No data for today'
    scored below three mild threshold wobbles.
    """
    reasons = ["No data for 2026-08-14"]
    assert alerting.classify(reasons, hard_failures=reasons) == "critical"


def test_hard_failure_promotes_a_two_reason_set():
    reasons = ["Volume collapse: 12 vs baseline 40000", "Failure spike: 0.9 vs 0.1"]
    assert alerting.classify(reasons, hard_failures=[reasons[0]]) == "critical"


def test_hard_failures_ignored_when_no_reasons():
    assert alerting.classify([], hard_failures=[]) == "ok"


# ---------------------------------------------------------------------------
# Notification gate / dedup
# ---------------------------------------------------------------------------


def test_warning_does_not_notify():
    send, _ = alerting.should_notify("warning", ["a"], state={}, now=NOW)
    assert send is False


def test_ok_does_not_notify():
    send, _ = alerting.should_notify("ok", [], state={}, now=NOW)
    assert send is False


def test_high_notifies_on_empty_state():
    send, new_state = alerting.should_notify("high", ["a", "b"], state={}, now=NOW)
    assert send is True
    assert new_state["last_severity"] == "high"
    assert new_state["last_sent"] == NOW.isoformat()


def test_identical_reason_set_suppressed_within_window():
    _, state = alerting.should_notify("high", ["a", "b"], state={}, now=NOW)
    send, _ = alerting.should_notify(
        "high", ["b", "a"], state=state, now=NOW + timedelta(hours=1)
    )
    assert send is False, "same conditions an hour later should not re-send"


def test_identical_reason_set_resends_after_window():
    _, state = alerting.should_notify("high", ["a", "b"], state={}, now=NOW)
    send, _ = alerting.should_notify(
        "high", ["a", "b"], state=state, now=NOW + timedelta(hours=7)
    )
    assert send is True


def test_escalation_bypasses_window():
    """high -> critical must page immediately, not wait out the 6h window."""
    _, state = alerting.should_notify("high", ["a", "b"], state={}, now=NOW)
    send, _ = alerting.should_notify(
        "critical", ["a", "b", "c"], state=state, now=NOW + timedelta(minutes=5)
    )
    assert send is True


def test_de_escalation_does_not_resend_within_window():
    _, state = alerting.should_notify("critical", ["a", "b", "c"], state={}, now=NOW)
    send, _ = alerting.should_notify(
        "critical", ["a", "b", "c"], state=state, now=NOW + timedelta(minutes=5)
    )
    assert send is False


def test_new_condition_appearing_sends_even_within_window():
    _, state = alerting.should_notify("high", ["a", "b"], state={}, now=NOW)
    send, _ = alerting.should_notify(
        "high", ["a", "c"], state=state, now=NOW + timedelta(minutes=5)
    )
    assert send is True, "a different failure mode is new information"


def test_drifting_metric_values_are_still_the_same_condition():
    """Reasons embed live numbers that change every run.

    Keying dedup on the whole string would make every evaluation look like a new
    problem, so the quiet window would never suppress anything.
    """
    first = ["Failure spike: fail 0.310 vs baseline 0.100"]
    later = ["Failure spike: fail 0.327 vs baseline 0.100"]
    _, state = alerting.should_notify("critical", first, state={}, now=NOW)
    send, _ = alerting.should_notify(
        "critical", later, state=state, now=NOW + timedelta(hours=1)
    )
    assert send is False


def test_corrupt_state_does_not_suppress():
    """A malformed state file must fail open (send), never fail closed (silent)."""
    send, _ = alerting.should_notify(
        "critical", ["a"], state={"last_sent": "not-a-timestamp"}, now=NOW
    )
    assert send is True


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    _, state = alerting.should_notify("critical", ["a", "b", "c"], state={}, now=NOW)
    alerting.save_state(path, state)
    assert alerting.load_state(path) == state


def test_load_missing_state_returns_empty(tmp_path):
    assert alerting.load_state(tmp_path / "nope.json") == {}


def test_load_corrupt_state_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    assert alerting.load_state(path) == {}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _result():
    return {
        "triggered": True,
        "severity": "critical",
        "reasons": ["No data for 2026-08-14"],
        "hard_failures": ["No data for 2026-08-14"],
        "today": {"total_measurements": 0, "reach_rate": 0.0, "fail_rate": 0.0},
        "baseline": {
            "total_measurements": 41230.0,
            "reach_rate": 0.7412,
            "fail_rate": 0.0913,
        },
    }


def test_subject_carries_severity_and_date():
    subject, _ = alerting.format_alert(_result(), day="2026-08-14")
    assert "CRITICAL" in subject
    assert "2026-08-14" in subject


def test_body_lists_every_reason():
    _, body = alerting.format_alert(_result(), day="2026-08-14")
    assert "No data for 2026-08-14" in body


def test_body_includes_baseline_for_comparison():
    _, body = alerting.format_alert(_result(), day="2026-08-14")
    assert "41230" in body or "41230.0" in body


def test_body_includes_dashboard_url_when_configured():
    _, body = alerting.format_alert(
        _result(), day="2026-08-14", dashboard_url="http://example.invalid:5050"
    )
    assert "http://example.invalid:5050" in body


def test_body_omits_dashboard_line_when_unset():
    _, body = alerting.format_alert(_result(), day="2026-08-14", dashboard_url="")
    assert "Dashboard:" not in body


# ---------------------------------------------------------------------------
# SMTP config
# ---------------------------------------------------------------------------


def test_smtp_config_incomplete_when_unset(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(var, raising=False)
    assert alerting.SmtpConfig.from_env().is_complete() is False


def test_smtp_config_complete_when_set(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "sender@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("ALERT_EMAIL_TO", "you@example.invalid")
    cfg = alerting.SmtpConfig.from_env()
    assert cfg.is_complete() is True
    assert cfg.recipients == ["you@example.invalid"]


def test_recipients_split_on_comma(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "sender@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("ALERT_EMAIL_TO", "a@example.invalid, b@example.invalid")
    assert alerting.SmtpConfig.from_env().recipients == [
        "a@example.invalid",
        "b@example.invalid",
    ]


def test_send_email_returns_false_when_unconfigured():
    cfg = alerting.SmtpConfig(host="", port=587, user="", password="", sender="", recipients=[])
    assert alerting.send_email("subj", "body", cfg) is False


def test_send_email_uses_starttls_and_returns_true(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["starttls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(alerting.smtplib, "SMTP", FakeSMTP)
    cfg = alerting.SmtpConfig(
        host="smtp.gmail.com",
        port=587,
        user="sender@example.invalid",
        password="pw",
        sender="sender@example.invalid",
        recipients=["you@example.invalid"],
    )
    assert alerting.send_email("subj", "body", cfg) is True
    assert sent["starttls"] is True
    assert sent["msg"]["To"] == "you@example.invalid"
    assert sent["msg"]["Subject"] == "subj"


def test_send_email_returns_false_on_smtp_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(alerting.smtplib, "SMTP", boom)
    cfg = alerting.SmtpConfig(
        host="smtp.gmail.com",
        port=587,
        user="u",
        password="p",
        sender="u",
        recipients=["you@example.invalid"],
    )
    assert alerting.send_email("subj", "body", cfg) is False


# ---------------------------------------------------------------------------
# State is only advanced on a successful send
# ---------------------------------------------------------------------------


def test_failed_send_does_not_burn_the_dedup_window(tmp_path, monkeypatch):
    """If SMTP fails, the next run must retry rather than think it already sent."""
    monkeypatch.setattr(alerting, "send_email", lambda *a, **k: False)
    path = tmp_path / "state.json"
    cfg = alerting.SmtpConfig(
        host="h", port=587, user="u", password="p", sender="u", recipients=["r@example.invalid"]
    )
    result = _result()

    assert alerting.notify(result, cfg, state_path=path, day="2026-08-14", now=NOW) is False
    assert alerting.load_state(path) == {}, "state must not advance on a failed send"

    monkeypatch.setattr(alerting, "send_email", lambda *a, **k: True)
    assert alerting.notify(result, cfg, state_path=path, day="2026-08-14", now=NOW) is True
    assert alerting.load_state(path)["last_severity"] == "critical"


# ---------------------------------------------------------------------------
# Deploy guards
# ---------------------------------------------------------------------------


def test_dockerfile_copies_alerting_module():
    """app.py imports alerting at module scope.

    If the image omits alerting.py the container fails at startup and takes the
    whole dashboard down -- not just alerting. Caught here rather than in prod.
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "COPY alerting.py" in dockerfile


def test_alert_runner_is_importable_without_gcp_credentials():
    """--test-email must work on a box with no ADC, so BigQuery imports stay lazy."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "cron" / "alert_runner.py"
    spec = importlib.util.spec_from_file_location("alert_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "main")
