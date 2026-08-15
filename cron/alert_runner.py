#!/usr/bin/env python3
"""Hourly revTr health check that emails on high/critical anomalies.

Why this exists separately from the dashboard: app.py only evaluates health
inside GET /api/health, so alerts previously fired only when a human happened
to load the page. An overnight outage with nobody watching sent nothing, which
is exactly the case the email is for. This runs on a cron whether or not anyone
is looking.

It imports the fetch/evaluate functions from app.py rather than reimplementing
them, so the cron and the dashboard can never disagree about a threshold. It
does NOT start the Flask server -- importing app.py only defines routes.

Cost: ~1.33 GiB scanned per run (3 queries against measurement-lab.revtr_raw),
billed to BQ_PROJECT (measurement-lab). Hourly is ~0.031 TiB/day.

Usage:
    python alert_runner.py                 # evaluate and email if warranted
    python alert_runner.py --dry-run       # evaluate and print, never send
    python alert_runner.py --test-email    # send a synthetic alert, no BigQuery
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone

# The container copies app.py to /app; the repo layout puts it one level up.
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerting  # noqa: E402


def _synthetic_result() -> dict:
    """A fake critical result, for verifying delivery without a real outage."""
    return {
        "triggered": True,
        "severity": "critical",
        "reasons": [
            "TEST ALERT: this is a delivery check, not a real anomaly",
            "TEST ALERT: no action required",
            "TEST ALERT: sent by alert_runner.py --test-email",
        ],
        "hard_failures": [],
        "today": {"total_measurements": 0, "reach_rate": 0.0, "fail_rate": 0.0},
        "baseline": {"total_measurements": 0, "reach_rate": 0.0, "fail_rate": 0.0},
        "baseline_days": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and print the verdict; never send email.",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a synthetic critical alert to verify SMTP. Skips BigQuery "
             "and bypasses the dedup window.",
    )
    parser.add_argument(
        "--target-day",
        default=date.today().isoformat(),
        help="Day to evaluate (YYYY-MM-DD). Default: today (UTC-ish, host tz).",
    )
    parser.add_argument(
        "--min-severity",
        default=os.getenv("ALERT_MIN_SEVERITY", "high"),
        choices=["warning", "high", "critical"],
        help="Lowest severity that triggers an email. Default: high.",
    )
    parser.add_argument(
        "--state-path",
        default=os.getenv("ALERT_STATE_PATH", str(alerting.DEFAULT_STATE_PATH)),
        help="Dedup state file. Must persist across runs to suppress repeats.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("alert_runner")

    cfg = alerting.SmtpConfig.from_env()
    dashboard_url = os.getenv("DASHBOARD_URL", "")

    if args.test_email:
        if not cfg.is_complete():
            log.error(
                "SMTP not configured. Need SMTP_HOST, SMTP_USER, SMTP_PASSWORD, "
                "ALERT_EMAIL_TO."
            )
            return 2
        subject, body = alerting.format_alert(
            _synthetic_result(), day=args.target_day, dashboard_url=dashboard_url
        )
        ok = alerting.send_email("[TEST] " + subject, body, cfg)
        log.info("Test email %s", "sent" if ok else "FAILED")
        return 0 if ok else 1

    # Imported late so --test-email needs no GCP credentials.
    import app  # noqa: E402

    target = date.fromisoformat(args.target_day)
    current_hour = datetime.now(timezone.utc).hour

    df = app.fetch_daily_health(target, app.BASELINE_DAYS)
    hourly_df = app.fetch_hourly_health(target, app.BASELINE_DAYS, current_hour)
    hop_df = app.fetch_hop_quality(target, app.BASELINE_DAYS)
    result = app.evaluate_health(df, target, hourly_df=hourly_df, hop_df=hop_df)

    log.info(
        "severity=%s triggered=%s reasons=%s",
        result.get("severity"),
        result.get("triggered"),
        json.dumps(result.get("reasons", [])),
    )

    if args.dry_run:
        subject, body = alerting.format_alert(
            result, day=args.target_day, dashboard_url=dashboard_url
        )
        print("--- would send (dry-run) ---" if result.get("triggered") else "--- healthy ---")
        print(subject)
        print(body)
        return 0

    sent = alerting.notify(
        result,
        cfg,
        state_path=args.state_path,
        day=args.target_day,
        dashboard_url=dashboard_url,
        min_severity=args.min_severity,
    )
    log.info("email_sent=%s", sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
