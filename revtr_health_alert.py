#!/usr/bin/env python3
"""
Monitor revTr health and send email alerts on regressions.

This script checks daily revTr volume + quality and alerts when:
  - total reverse traceroutes drop too much vs baseline
  - reachability rate drops vs baseline
  - failure rate spikes vs baseline

Run manually or from cron:
  python code/analysis/revtr_monitoring/revtr_health_alert.py --email-to you@example.com
"""

from __future__ import annotations

import argparse
import os
import smtplib
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from statistics import median
from typing import Optional

import pandas as pd
from google.cloud import bigquery

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


@dataclass
class AlertThresholds:
    volume_drop_ratio: float = 0.5
    quality_drop_ratio: float = 0.75
    quality_drop_abs: float = 0.1
    fail_rate_increase_abs: float = 0.1


def _build_daily_health_query(start_date: date, end_date: date) -> str:
    # End-date is exclusive.
    return f"""
    SELECT
      DATE(t.date) AS day,
      COUNT(*) AS total_measurements,
      COUNTIF(t.raw.stop_reason = 'REACHES') AS reaches_count,
      COUNTIF(t.raw.fail_reason IS NOT NULL) AS failed_count
    FROM `measurement-lab.revtr_raw.revtr1` t
    WHERE t.date >= DATE('{start_date.isoformat()}')
      AND t.date < DATE('{end_date.isoformat()}')
    GROUP BY day
    ORDER BY day
    """


def fetch_daily_health(
    client: bigquery.Client,
    target_day: date,
    baseline_days: int,
) -> pd.DataFrame:
    start = target_day - timedelta(days=baseline_days)
    end = target_day + timedelta(days=1)
    q = _build_daily_health_query(start, end)
    df = client.query(q).to_dataframe()
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["reach_rate"] = df["reaches_count"] / df["total_measurements"].clip(lower=1)
    df["fail_rate"] = df["failed_count"] / df["total_measurements"].clip(lower=1)
    return df


def evaluate_health(
    df: pd.DataFrame,
    target_day: date,
    thresholds: AlertThresholds,
) -> tuple[bool, str]:
    day_row = df[df["day"] == target_day]
    baseline = df[df["day"] < target_day]

    if day_row.empty:
        return True, f"No revTr data for target day {target_day}."
    if baseline.empty:
        return True, "No baseline data available. Increase baseline window."

    current = day_row.iloc[0]
    baseline_total = float(median(baseline["total_measurements"]))
    baseline_reach = float(median(baseline["reach_rate"]))
    baseline_fail = float(median(baseline["fail_rate"]))

    reasons: list[str] = []
    total_now = float(current["total_measurements"])
    reach_now = float(current["reach_rate"])
    fail_now = float(current["fail_rate"])

    if baseline_total > 0 and total_now < baseline_total * thresholds.volume_drop_ratio:
        reasons.append(
            f"volume drop: {total_now:.0f} vs baseline median {baseline_total:.0f}"
        )

    quality_ratio_trigger = baseline_reach * thresholds.quality_drop_ratio
    quality_abs_trigger = baseline_reach - thresholds.quality_drop_abs
    quality_trigger = min(quality_ratio_trigger, quality_abs_trigger)
    if reach_now < quality_trigger:
        reasons.append(
            "quality drop: "
            f"reach_rate {reach_now:.3f} vs baseline median {baseline_reach:.3f}"
        )

    if fail_now > baseline_fail + thresholds.fail_rate_increase_abs:
        reasons.append(
            "failure spike: "
            f"fail_rate {fail_now:.3f} vs baseline median {baseline_fail:.3f}"
        )

    lines = [
        f"Target day: {target_day}",
        f"Current total_measurements: {int(total_now)}",
        f"Current reach_rate: {reach_now:.4f}",
        f"Current fail_rate: {fail_now:.4f}",
        "",
        "Baseline medians:",
        f"- total_measurements: {baseline_total:.1f}",
        f"- reach_rate: {baseline_reach:.4f}",
        f"- fail_rate: {baseline_fail:.4f}",
    ]
    if reasons:
        lines += ["", "Triggered conditions:"] + [f"- {r}" for r in reasons]
    else:
        lines += ["", "No alert conditions triggered."]

    return bool(reasons), "\n".join(lines)


def send_email(subject: str, body: str, to_emails: list[str]) -> None:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM", smtp_user or "")

    if not smtp_host or not smtp_user or not smtp_password or not from_email:
        raise RuntimeError(
            "Missing SMTP config. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg.set_content(body)

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alert on revTr volume/quality regressions."
    )
    parser.add_argument(
        "--target-day",
        type=str,
        default=date.today().isoformat(),
        help="Day to evaluate (YYYY-MM-DD), default: today.",
    )
    parser.add_argument(
        "--baseline-days",
        type=int,
        default=7,
        help="How many prior days to use as baseline median.",
    )
    parser.add_argument(
        "--volume-drop-ratio",
        type=float,
        default=0.5,
        help="Alert when volume < baseline_median * this ratio.",
    )
    parser.add_argument(
        "--quality-drop-ratio",
        type=float,
        default=0.75,
        help="Alert when reach_rate < baseline_median * this ratio.",
    )
    parser.add_argument(
        "--quality-drop-abs",
        type=float,
        default=0.1,
        help="Alert when reach_rate drops by at least this absolute amount.",
    )
    parser.add_argument(
        "--fail-rate-increase-abs",
        type=float,
        default=0.1,
        help="Alert when fail_rate exceeds baseline by this absolute amount.",
    )
    parser.add_argument(
        "--bq-project",
        type=str,
        default="measurement-lab",
        help="BigQuery project for querying revtr_raw.revtr1.",
    )
    parser.add_argument(
        "--email-to",
        type=str,
        default=os.getenv("ALERT_EMAIL_TO", ""),
        help="Comma-separated alert recipients. Can also set ALERT_EMAIL_TO env var.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print output only; do not send emails.",
    )
    args = parser.parse_args()

    if load_dotenv is not None:
        load_dotenv()

    target_day = date.fromisoformat(args.target_day)
    recipients = [x.strip() for x in args.email_to.split(",") if x.strip()]

    thresholds = AlertThresholds(
        volume_drop_ratio=args.volume_drop_ratio,
        quality_drop_ratio=args.quality_drop_ratio,
        quality_drop_abs=args.quality_drop_abs,
        fail_rate_increase_abs=args.fail_rate_increase_abs,
    )

    client = bigquery.Client(project=args.bq_project)
    df = fetch_daily_health(client, target_day=target_day, baseline_days=args.baseline_days)
    triggered, report = evaluate_health(df, target_day=target_day, thresholds=thresholds)

    print(report)

    if not triggered:
        return

    subject = f"[ALERT] revTr health regression on {target_day.isoformat()}"
    if args.dry_run:
        print("\nDry-run: alert was triggered; no email sent.")
        return

    if not recipients:
        raise RuntimeError(
            "Alert triggered but no recipients provided. Set --email-to or ALERT_EMAIL_TO."
        )
    send_email(subject=subject, body=report, to_emails=recipients)
    print(f"\nAlert email sent to: {', '.join(recipients)}")


if __name__ == "__main__":
    main()
