#!/usr/bin/env python3
"""Severity classification and email delivery for revTr health alerts.

Single source of truth for "is this bad enough to email, and have we already
said so?". Both the Flask app (on /api/health) and the hourly cron
(cron/alert_runner.py) go through here, so the two can never disagree about a
threshold and can never double-send: dedup state lives in one file on disk
rather than in a process global.

Configuration is entirely by env var, so the app password stays out of git:

    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
    SMTP_SECURITY        "starttls" (587), "ssl" (465), "none"; inferred from
                         SMTP_PORT when unset. Any provider speaking SMTP works.
    ALERT_EMAIL_TO       comma-separated recipients
    ALERT_STATE_PATH     dedup state file (default ~/.revtr_alert_state.json)
    ALERT_MIN_SEVERITY   lowest severity that emails (default "high")
    ALERT_REPEAT_HOURS   re-send an unchanged condition after N hours (default 6)
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Ordered worst-last so escalation is a simple comparison.
SEVERITY_ORDER = {"ok": 0, "warning": 1, "high": 2, "critical": 3}

DEFAULT_STATE_PATH = Path(
    os.getenv("ALERT_STATE_PATH", str(Path.home() / ".revtr_alert_state.json"))
)
MIN_SEVERITY = os.getenv("ALERT_MIN_SEVERITY", "high")
REPEAT_AFTER_HOURS = float(os.getenv("ALERT_REPEAT_HOURS", "6"))


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def classify(reasons: list[str], hard_failures: Iterable[str] = ()) -> str:
    """Map triggered conditions to a severity.

    Count alone is a bad proxy for seriousness: a total data outage raises
    exactly one condition ("No data for <day>") and would score below three
    mild threshold wobbles. So callers tag genuine hard failures separately and
    those go straight to critical regardless of how many fired.
    """
    if not reasons:
        return "ok"
    if list(hard_failures):
        return "critical"
    if len(reasons) >= 3:
        return "critical"
    if len(reasons) == 2:
        return "high"
    return "warning"


# ---------------------------------------------------------------------------
# Dedup state
# ---------------------------------------------------------------------------


def _fingerprint(reasons: list[str]) -> str:
    """Order-independent identity for a set of triggered conditions.

    Reasons embed live metric values ("fail 0.31 vs baseline 0.10"), which drift
    every run, so we key on the condition prefix before the colon rather than
    the whole string. Otherwise every evaluation looks like a brand-new problem
    and the dedup window never suppresses anything.
    """
    keys = sorted(r.split(":", 1)[0].strip() for r in reasons)
    return "|".join(keys)


def load_state(path: str | Path) -> dict[str, Any]:
    """Read dedup state. A missing or corrupt file is treated as 'no state'."""
    try:
        with open(path) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read alert state at %s (%s); treating as empty", path, e)
        return {}


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    """Write dedup state atomically so a crash mid-write can't corrupt it."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        # Losing state means the next alert re-sends. That is the right way to
        # fail: noisier, never silent.
        log.error("Could not write alert state to %s: %s", path, e)


def should_notify(
    severity: str,
    reasons: list[str],
    state: dict[str, Any],
    now: datetime,
    min_severity: str = MIN_SEVERITY,
    repeat_after_hours: float = REPEAT_AFTER_HOURS,
) -> tuple[bool, dict[str, Any]]:
    """Decide whether to email, and return the state to persist if we do.

    Returns (send, new_state). The caller must only persist new_state after the
    send actually succeeds — otherwise a failed SMTP call would burn the dedup
    window and the alert would be silently swallowed.
    """
    if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(min_severity, 2):
        return False, state

    new_state = {
        "last_sent": now.isoformat(),
        "last_severity": severity,
        "last_fingerprint": _fingerprint(reasons),
    }

    if not state:
        return True, new_state

    # Escalation is always news, even inside the quiet window.
    prev_sev = SEVERITY_ORDER.get(state.get("last_severity", "ok"), 0)
    if SEVERITY_ORDER.get(severity, 0) > prev_sev:
        return True, new_state

    # A different set of conditions is a different failure mode.
    if state.get("last_fingerprint") != new_state["last_fingerprint"]:
        return True, new_state

    try:
        last_sent = datetime.fromisoformat(state["last_sent"])
    except (KeyError, TypeError, ValueError):
        # Unparseable state: fail open rather than suppress a real alert.
        return True, new_state

    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)

    if now - last_sent >= timedelta(hours=repeat_after_hours):
        return True, new_state

    return False, state


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_alert(
    result: dict[str, Any],
    day: str,
    dashboard_url: str = "",
) -> tuple[str, str]:
    """Render an evaluate_health() result as (subject, plain-text body)."""
    severity = str(result.get("severity", "warning")).upper()
    reasons = result.get("reasons", [])
    hard = set(result.get("hard_failures", []))
    today = result.get("today", {})
    baseline = result.get("baseline", {})

    plural = "s" if len(reasons) != 1 else ""
    lines = [
        f"Severity: {severity} ({len(reasons)} condition{plural} triggered)",
        f"Day: {day}",
        "",
        "Triggered conditions:",
    ]
    for r in reasons:
        marker = "  [HARD] " if r in hard else "  - "
        lines.append(f"{marker}{r}")

    lines += [
        "",
        "Today:",
        f"  Total measurements: {today.get('total_measurements', '?')}",
        f"  Reach rate: {today.get('reach_rate', '?')}",
        f"  Fail rate: {today.get('fail_rate', '?')}",
    ]
    if "hourly_volume" in result:
        hv = result["hourly_volume"]
        lines.append(
            f"  Volume so far today: {hv.get('today')} "
            f"(baseline median at this hour: {hv.get('baseline_median')})"
        )
    if "type12" in result:
        t12 = result["type12"]
        lines.append(
            f"  Interdomain assumption fraction: {t12.get('today')} "
            f"(baseline median: {t12.get('baseline_median')})"
        )

    n_base = result.get("baseline_days", 0)
    lines += [
        "",
        f"Baseline medians (over {n_base} day{'s' if n_base != 1 else ''} "
        "actually present in the window):",
        f"  Total measurements: {baseline.get('total_measurements', '?')}",
        f"  Reach rate: {baseline.get('reach_rate', '?')}",
        f"  Fail rate: {baseline.get('fail_rate', '?')}",
    ]
    if dashboard_url:
        lines += ["", f"Dashboard: {dashboard_url}"]

    return f"[{severity}] revTr health alert — {day}", "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: list[str] = field(default_factory=list)
    timeout: float = 30.0
    # "starttls" (587), "ssl" (465, implicit TLS), "none", or "" to infer.
    # Providers differ on this and getting it wrong raises mid-send, which
    # loses the alert -- so infer from the port unless told otherwise.
    security: str = ""

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        user = os.getenv("SMTP_USER", "")
        raw_to = os.getenv("ALERT_EMAIL_TO", "")
        return cls(
            host=os.getenv("SMTP_HOST", ""),
            port=int(os.getenv("SMTP_PORT", "587")),
            user=user,
            password=os.getenv("SMTP_PASSWORD", ""),
            sender=os.getenv("SMTP_FROM", user),
            recipients=[x.strip() for x in raw_to.split(",") if x.strip()],
            timeout=float(os.getenv("SMTP_TIMEOUT", "30")),
            security=os.getenv("SMTP_SECURITY", ""),
        )

    def resolved_security(self) -> str:
        """Explicit setting wins; otherwise port 465 means implicit TLS."""
        if self.security:
            return self.security.strip().lower()
        return "ssl" if self.port == 465 else "starttls"

    def is_complete(self) -> bool:
        return bool(self.host and self.user and self.password and self.recipients)


def send_email(subject: str, body: str, cfg: SmtpConfig) -> bool:
    """Send one alert. Returns True only if SMTP accepted the message.

    Never raises: an alerting path that can crash the caller is worse than one
    that logs and reports failure. The bool is what gates the dedup state.
    """
    if not cfg.is_complete():
        log.warning("SMTP not fully configured; alert email not sent")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(body)

    security = cfg.resolved_security()
    try:
        opener = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        with opener(cfg.host, cfg.port, timeout=cfg.timeout) as server:
            if security == "starttls":
                server.starttls()
            server.login(cfg.user, cfg.password)
            server.send_message(msg)
    except Exception as e:  # noqa: BLE001 - alerting must not raise
        log.error("Failed to send alert email: %s: %s", type(e).__name__, e)
        return False

    log.info("Alert email sent to %s", ", ".join(cfg.recipients))
    return True


def notify(
    result: dict[str, Any],
    cfg: SmtpConfig,
    state_path: str | Path = DEFAULT_STATE_PATH,
    day: str | None = None,
    now: datetime | None = None,
    dashboard_url: str = "",
    min_severity: str = MIN_SEVERITY,
    repeat_after_hours: float = REPEAT_AFTER_HOURS,
) -> bool:
    """Email about `result` if it is severe enough and not a repeat.

    Returns True if an email was actually sent.
    """
    now = now or datetime.now(timezone.utc)
    day = day or now.date().isoformat()

    severity = result.get("severity", "ok")
    reasons = result.get("reasons", [])

    send, new_state = should_notify(
        severity,
        reasons,
        state=load_state(state_path),
        now=now,
        min_severity=min_severity,
        repeat_after_hours=repeat_after_hours,
    )
    if not send:
        return False

    subject, body = format_alert(result, day=day, dashboard_url=dashboard_url)
    if not send_email(subject, body, cfg):
        # Deliberately do NOT persist state: retry on the next run.
        return False

    save_state(state_path, new_state)
    return True
