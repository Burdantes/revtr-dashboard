#!/usr/bin/env python3
"""
Lightweight Flask dashboard for monitoring revTr health.

Run:
    python code/analysis/revtr_monitoring/app.py

Requires:
    - Google Cloud credentials (gcloud auth application-default login)
    - pip install flask google-cloud-bigquery pandas requests python-dotenv
"""

from __future__ import annotations

import ipaddress
import logging
import os
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from statistics import median
from typing import Any

import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request
from google.cloud import bigquery

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Provide at runtime via the REVTR_API_KEY env var (no default in source so the
# key stays out of version control). /api/ping degrades gracefully if unset.
REVTR_API_KEY = os.getenv("REVTR_API_KEY")
REVTR_BASE_URL = os.getenv(
    "REVTR_BASE_URL", "https://revtr.ccs.neu.edu:8080/api/v1"
)
BQ_PROJECT = os.getenv("BQ_PROJECT", "measurement-lab")
BASELINE_DAYS = int(os.getenv("REVTR_BASELINE_DAYS", "7"))

# Long-range panels read the pre-aggregated rollup table. All queries (raw
# revtr_raw scans and these summary reads) bill to BQ_PROJECT (measurement-lab);
# the rollup table lives in nsf and is read cross-project. See fetch_summary.
RANGE_DAYS = {"7d": 7, "30d": 30, "1y": 365}
SUMMARY_TABLE = os.getenv(
    "SUMMARY_TABLE", "nsf-2148275-66720.revtr_dashboard.daily_summary"
)

# Per-destination-AS-per-day distribution panel. The heavy longest-prefix match
# (hopannotation2 + the full RouteViews prefix table, ~3 min) is precomputed
# DAILY by the VM cron (cron/as_dist_runner.py -> as_distribution_7d); the
# endpoint just reads that small table. AS_DIST_TABLE is read cross-project from
# nsf. Result is cached in-process for AS_DIST_CACHE_TTL seconds.
AS_DIST_TABLE = os.getenv(
    "AS_DIST_TABLE", "nsf-2148275-66720.revtr_dashboard.as_distribution_7d"
)
AS_DIST_CACHE_TTL = float(os.getenv("AS_DIST_CACHE_TTL", "3600"))

# Per-hop-type breakdown for the Hop Composition panel. Maps an API key to its
# daily_summary column and a human-readable interpretation (from the M-Lab
# revTr blogpost's hop-type taxonomy).
HOP_TYPE_COLUMNS = {
    "type1": "type1_hops",
    "type3": "type3_hops",
    "type4": "type4_hops",
    "type5": "type5_hops",
    "type6": "type6_hops",
    "intradomain_assumed": "intradomain_assumed_hops",
    "interdomain_assumed": "interdomain_assumed_hops",
}
HOP_TYPE_LABELS = {
    "type1": "Destination (type 1)",
    "type3": "Intersected Traceroute (type 3)",
    "type4": "Intersected RR-Atlas (type 4)",
    "type5": "Record-Route (type 5)",
    "type6": "Spoofed Record-Route (type 6)",
    "intradomain_assumed": "Intradomain (type 11)",
    "interdomain_assumed": "Interdomain (type 12)",
}


def _range_to_window(range_str: str, today: date) -> tuple[date, date]:
    """Map a range key to an inclusive (start, end=today) date window."""
    if range_str == "all":
        return date(2000, 1, 1), today
    days = RANGE_DAYS.get(range_str, 7)
    return today - timedelta(days=days - 1), today


def _granularity_for_range(range_str: str) -> str:
    """Time bucket for a range so long-range charts stay readable.

    7d/30d -> daily; 1y -> weekly; all -> monthly.
    """
    if range_str == "1y":
        return "week"
    if range_str == "all":
        return "month"
    return "day"


def _bucket_label(d: date, gran: str) -> str:
    """Bucket key/label for a day at the given granularity.

    week -> the Monday of that week (ISO date); month -> ``YYYY-MM``; day -> ISO date.
    Chronological string sort matches chronological order in every case.
    """
    if gran == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if gran == "month":
        return f"{d.year:04d}-{d.month:02d}"
    return d.isoformat()


def _bucket_sums(summary: pd.DataFrame, gran: str, cols: list[str]) -> list[tuple[str, dict[str, int]]]:
    """Aggregate daily summary rows into time buckets, summing ``cols``.

    Returns an ordered (chronological) list of ``(bucket_label, {col: sum})``.
    Fractions must be recomputed by the caller from the summed numerators/denominators
    (averaging per-day fractions would ignore per-day volume).
    """
    buckets: dict[str, dict[str, int]] = {}
    order: list[str] = []
    for _, row in summary.iterrows():
        label = _bucket_label(row["day"], gran)
        if label not in buckets:
            buckets[label] = {c: 0 for c in cols}
            order.append(label)
        for c in cols:
            buckets[label][c] += _safe_int(row[c])
    return [(lbl, buckets[lbl]) for lbl in order]


# Raw revtr fail_reason strings collapse into a handful of primary failure modes.
# Order matters: check the most specific / dominant tokens first.
def _categorize_failure(reason: str) -> str:
    """Bucket a raw revtr ``fail_reason`` string into a primary failure category."""
    r = (reason or "").lower()
    if "gaplimit" in r:
        return "Gap limit reached"
    if "timed out" in r or "timeout" in r:
        return "Timed out"
    if "unreach" in r:
        return "Unreachable"
    if "loop" in r:
        return "Routing loop"
    return "Probe/system error"

# Alert thresholds (same defaults as revtr_health_alert.py)
VOLUME_DROP_RATIO = 0.5
QUALITY_DROP_RATIO = 0.75
QUALITY_DROP_ABS = 0.1
FAIL_RATE_INCREASE_ABS = 0.1

# Set these at runtime via env vars (kept out of source so the repo can be public).
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "")  # e.g. http://your-host:5050 (shown in alert emails)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

log = logging.getLogger(__name__)

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------------------------
# BigQuery helpers
# ---------------------------------------------------------------------------

_bq_client: bigquery.Client | None = None


def get_bq_client() -> bigquery.Client:
    global _bq_client
    if _bq_client is None:
        _bq_client = bigquery.Client(project=BQ_PROJECT)
    return _bq_client


# Hard timeouts so a stalled BigQuery call can never wedge a worker thread.
# With only a couple of gunicorn threads, two un-timed-out queries blocking
# forever takes the whole server down (it accepts connections but never
# responds). query(timeout=) caps the RPC that starts the job;
# result(timeout=) caps how long we wait for the job to finish.
BQ_QUERY_RPC_TIMEOUT = float(os.getenv("BQ_QUERY_RPC_TIMEOUT", "30"))
BQ_RESULT_TIMEOUT = float(os.getenv("BQ_RESULT_TIMEOUT", "90"))


def run_query(query: str) -> pd.DataFrame:
    """Run a BigQuery query with hard timeouts and return a DataFrame.

    Raises concurrent.futures.TimeoutError (or google.api_core errors) if the
    query stalls, instead of blocking the worker thread indefinitely.
    """
    client = get_bq_client()
    job = client.query(query, timeout=BQ_QUERY_RPC_TIMEOUT)
    return job.result(timeout=BQ_RESULT_TIMEOUT).to_dataframe()


def _safe_int(v) -> int:
    """int() that treats SQL NULL as 0. BigQuery INT64 NULLs arrive as
    pandas NA (NAType), None, or NaN depending on dtype; pd.isna covers all."""
    return 0 if v is None or pd.isna(v) else int(v)


def fetch_summary(start: date, end: date) -> pd.DataFrame:
    """Read per-day rows from daily_summary for an inclusive window.

    The query bills to BQ_PROJECT (measurement-lab) and reads the rollup table
    cross-project from nsf; the dashboard identity has dataViewer on that dataset.
    """
    query = f"""
    SELECT *
    FROM `{SUMMARY_TABLE}`
    WHERE day BETWEEN DATE('{start.isoformat()}') AND DATE('{end.isoformat()}')
    ORDER BY day
    """
    df = run_query(query)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    return df


def fetch_daily_health(target_day: date, baseline_days: int) -> pd.DataFrame:
    """Fetch daily revTr metrics for target_day and its baseline window."""
    start = target_day - timedelta(days=baseline_days)
    end = target_day + timedelta(days=1)
    query = f"""
    SELECT
      DATE(t.date) AS day,
      COUNT(*) AS total_measurements,
      COUNTIF(t.raw.stop_reason = 'REACHES') AS reaches_count,
      COUNTIF(t.raw.fail_reason IS NOT NULL AND t.raw.stop_reason != 'REACHES') AS failed_count
    FROM `measurement-lab.revtr_raw.revtr1` t
    WHERE t.date >= DATE('{start.isoformat()}')
      AND t.date < DATE('{end.isoformat()}')
      AND NOT (NET.IP_FROM_STRING(t.raw.dst) BETWEEN
               NET.IP_FROM_STRING('34.0.0.0') AND NET.IP_FROM_STRING('34.255.255.255'))
    GROUP BY day
    ORDER BY day
    """
    df = run_query(query)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["reach_rate"] = df["reaches_count"] / df["total_measurements"].clip(lower=1)
    df["fail_rate"] = df["failed_count"] / df["total_measurements"].clip(lower=1)
    return df


def fetch_queries_today(target_day: date) -> int:
    """Count the number of revTr queries that were submitted today."""
    query = f"""
    SELECT COUNT(*) AS cnt
    FROM `measurement-lab.revtr_raw.revtr1` t
    WHERE DATE(t.date) = DATE('{target_day.isoformat()}')
      AND NOT (NET.IP_FROM_STRING(t.raw.dst) BETWEEN
               NET.IP_FROM_STRING('34.0.0.0') AND NET.IP_FROM_STRING('34.255.255.255'))
    """
    result = run_query(query)
    if result.empty:
        return 0
    return int(result.iloc[0]["cnt"])


def fetch_hop_quality(target_day: date, baseline_days: int) -> pd.DataFrame:
    """Fraction of reaching measurements with interdomain symmetry / suspect RR-atlas hop."""
    start = target_day - timedelta(days=baseline_days)
    end = target_day + timedelta(days=1)
    query = f"""
    WITH per_measurement AS (
      SELECT
        DATE(t.date) AS day,
        EXISTS(
          SELECT 1
          FROM UNNEST(t.raw.revtr_hops) h
          WHERE h.hop_type IN (11, 12)
            AND h.asn IS NOT NULL
            AND h.asn != IFNULL((
              SELECT h2.asn
              FROM UNNEST(t.raw.revtr_hops) h2
              WHERE h2.hop_number < h.hop_number AND h2.asn IS NOT NULL
              ORDER BY h2.hop_number DESC
              LIMIT 1
            ), h.asn)
        ) AS has_interdomain,
        EXISTS(
          SELECT 1
          FROM UNNEST(t.raw.revtr_hops) h
          WHERE h.hop_type = 12
            AND h.asn IS NOT NULL
            AND h.asn != IFNULL((
              SELECT h2.asn
              FROM UNNEST(t.raw.revtr_hops) h2
              WHERE h2.hop_number < h.hop_number AND h2.asn IS NOT NULL
              ORDER BY h2.hop_number DESC
              LIMIT 1
            ), h.asn)
        ) AS has_type12,
        -- Suspect RR-atlas: a type-4 hop creates an AS-path loop that clears
        -- when type-4 hops are removed. (Live query checks AS-level only; the
        -- daily_summary rollup additionally checks the geographic path.)
        (
          (SELECT COUNTIF(h.hop_type = 4) FROM UNNEST(t.raw.revtr_hops) h) > 0
          AND `nsf-2148275-66720.revtr_dashboard.has_loop`(
                ARRAY(SELECT CAST(h.asn AS STRING) FROM UNNEST(t.raw.revtr_hops) h
                      WHERE h.asn IS NOT NULL ORDER BY h.hop_number))
          AND NOT `nsf-2148275-66720.revtr_dashboard.has_loop`(
                ARRAY(SELECT CAST(h.asn AS STRING) FROM UNNEST(t.raw.revtr_hops) h
                      WHERE h.asn IS NOT NULL AND h.hop_type != 4 ORDER BY h.hop_number))
        ) AS has_suspect_rr_atlas
      FROM `measurement-lab.revtr_raw.revtr1` t
      WHERE t.date >= DATE('{start.isoformat()}')
        AND t.date < DATE('{end.isoformat()}')
        AND t.raw.stop_reason = 'REACHES'
        AND NOT (NET.IP_FROM_STRING(t.raw.dst) BETWEEN
                 NET.IP_FROM_STRING('34.0.0.0') AND NET.IP_FROM_STRING('34.255.255.255'))
    )
    SELECT
      day,
      COUNT(*) AS total_reaching,
      COUNTIF(has_interdomain) AS interdomain_count,
      COUNTIF(has_type12) AS type12_count,
      COUNTIF(has_suspect_rr_atlas) AS suspect_rr_atlas_count
    FROM per_measurement
    GROUP BY day
    ORDER BY day
    """
    df = run_query(query)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["frac_interdomain"] = df["interdomain_count"] / df["total_reaching"].clip(lower=1)
    df["frac_type12"] = df["type12_count"] / df["total_reaching"].clip(lower=1)
    df["frac_suspect_rr_atlas"] = df["suspect_rr_atlas_count"] / df["total_reaching"].clip(lower=1)
    return df


def fetch_as_distribution() -> pd.DataFrame:
    """Read the precomputed per-destination-AS-per-day table (one row per AS/day).

    The heavy longest-prefix match (hopannotation2 + the full RouteViews prefix
    table, ~3 min) is run DAILY by ``cron/as_dist_runner.py`` and written to
    ``AS_DIST_TABLE``; this just reads it, so the request stays well under the
    worker timeout. The destination AS is resolved by longest-prefix-matching
    ``raw.dst`` (covering failing measurements too, so the per-AS reach fraction
    is meaningful — unlike deriving it from the type-1 destination hop, which
    only exists for reaching measurements). Destinations not covered by any
    prefix collapse into an ``"unmapped"`` row; the GCP-client range
    (``34.0.0.0/8``), excluded from every other panel, is a single ``"gcp"`` row
    per day. Covers the last 7 full days.
    """
    query = f"""
    SELECT asn, as_name, is_gcp,
      FORMAT_DATE('%Y-%m-%d', day) AS day,
      unique_ips, tests, reaches, interdomain_count,
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', computed_at) AS computed_at
    FROM `{AS_DIST_TABLE}`
    ORDER BY tests DESC
    """
    return run_query(query)


def fetch_hourly_health(target_day: date, baseline_days: int, current_hour: int) -> pd.DataFrame:
    """Fetch per-day counts truncated to current_hour for fair comparison."""
    start = target_day - timedelta(days=baseline_days)
    end = target_day + timedelta(days=1)
    query = f"""
    SELECT
      DATE(t.date) AS day,
      COUNT(*) AS total_measurements,
      COUNTIF(t.raw.stop_reason = 'REACHES') AS reaches_count,
      COUNTIF(t.raw.fail_reason IS NOT NULL AND t.raw.stop_reason != 'REACHES') AS failed_count
    FROM `measurement-lab.revtr_raw.revtr1` t
    WHERE t.date >= DATE('{start.isoformat()}')
      AND t.date < DATE('{end.isoformat()}')
      AND NOT (NET.IP_FROM_STRING(t.raw.dst) BETWEEN
               NET.IP_FROM_STRING('34.0.0.0') AND NET.IP_FROM_STRING('34.255.255.255'))
      AND EXTRACT(HOUR FROM TIMESTAMP_SECONDS(t.raw.date)) <= {current_hour}
    GROUP BY day
    ORDER BY day
    """
    df = run_query(query)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["reach_rate"] = df["reaches_count"] / df["total_measurements"].clip(lower=1)
    df["fail_rate"] = df["failed_count"] / df["total_measurements"].clip(lower=1)
    return df


def evaluate_health(
    df: pd.DataFrame,
    target_day: date,
    hourly_df: pd.DataFrame | None = None,
    hop_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Return health evaluation dict for JSON serialization."""
    day_row = df[df["day"] == target_day]
    baseline = df[df["day"] < target_day]

    result: dict[str, Any] = {"triggered": False, "reasons": [], "today": {}, "baseline": {}}

    if day_row.empty:
        result["triggered"] = True
        result["reasons"] = [f"No data for {target_day}"]
        return result

    current = day_row.iloc[0]
    result["today"] = {
        "total_measurements": int(current["total_measurements"]),
        "reaches_count": int(current["reaches_count"]),
        "failed_count": int(current["failed_count"]),
        "reach_rate": round(float(current["reach_rate"]), 4),
        "fail_rate": round(float(current["fail_rate"]), 4),
    }

    if baseline.empty:
        result["baseline"] = {}
        return result

    baseline_total = float(median(baseline["total_measurements"]))
    baseline_reach = float(median(baseline["reach_rate"]))
    baseline_fail = float(median(baseline["fail_rate"]))

    result["baseline"] = {
        "total_measurements": round(baseline_total, 1),
        "reach_rate": round(baseline_reach, 4),
        "fail_rate": round(baseline_fail, 4),
    }

    reasons: list[str] = []

    # --- Hour-aware volume check ---
    if hourly_df is not None and not hourly_df.empty:
        h_today = hourly_df[hourly_df["day"] == target_day]
        h_baseline = hourly_df[hourly_df["day"] < target_day]
        if not h_today.empty and not h_baseline.empty:
            vol_now = float(h_today.iloc[0]["total_measurements"])
            vol_baseline = float(median(h_baseline["total_measurements"]))
            result["hourly_volume"] = {
                "today": round(vol_now),
                "baseline_median": round(vol_baseline, 1),
            }
            if vol_baseline > 0 and vol_now < vol_baseline * VOLUME_DROP_RATIO:
                reasons.append(
                    f"Volume drop (hour-adjusted): {vol_now:.0f} vs baseline {vol_baseline:.0f}"
                )
    else:
        # Fallback to full-day comparison
        total_now = float(current["total_measurements"])
        if baseline_total > 0 and total_now < baseline_total * VOLUME_DROP_RATIO:
            reasons.append(f"Volume drop: {total_now:.0f} vs baseline {baseline_total:.0f}")

    # --- Reach rate drop ---
    reach_now = float(current["reach_rate"])
    quality_trigger = min(
        baseline_reach * QUALITY_DROP_RATIO,
        baseline_reach - QUALITY_DROP_ABS,
    )
    if reach_now < quality_trigger:
        reasons.append(f"Quality drop: reach {reach_now:.3f} vs baseline {baseline_reach:.3f}")

    # --- Reach rate spike ---
    if baseline_reach > 0 and reach_now > baseline_reach + QUALITY_DROP_ABS:
        reasons.append(
            f"Reach rate spike: {reach_now:.3f} vs baseline {baseline_reach:.3f}"
        )

    # --- Fail rate spike ---
    fail_now = float(current["fail_rate"])
    if fail_now > baseline_fail + FAIL_RATE_INCREASE_ABS:
        reasons.append(f"Failure spike: fail {fail_now:.3f} vs baseline {baseline_fail:.3f}")

    # --- Interdomain assumption fraction spike ---
    if hop_df is not None and not hop_df.empty:
        hq_today = hop_df[hop_df["day"] == target_day]
        hq_baseline = hop_df[hop_df["day"] < target_day]
        if not hq_today.empty and not hq_baseline.empty:
            type12_now = float(hq_today.iloc[0]["frac_type12"])
            type12_baseline = float(median(hq_baseline["frac_type12"]))
            result["type12"] = {
                "today": round(type12_now, 4),
                "baseline_median": round(type12_baseline, 4),
            }
            if type12_now > type12_baseline + QUALITY_DROP_ABS:
                reasons.append(
                    f"Interdomain assumption spike: {type12_now:.3f} vs baseline {type12_baseline:.3f}"
                )

    result["triggered"] = bool(reasons)
    result["reasons"] = reasons

    # Severity: 1 flag = warning, 2 = high, 3+ = critical
    n = len(reasons)
    result["severity"] = "critical" if n >= 3 else "high" if n == 2 else "warning" if n == 1 else "ok"

    return result


def send_alert_email(result: dict[str, Any]) -> None:
    """Send an alert email if SMTP is configured."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        log.info("SMTP not configured, skipping alert email")
        return

    severity = result.get("severity", "warning").upper()
    reasons = result.get("reasons", [])
    today_data = result.get("today", {})
    baseline_data = result.get("baseline", {})

    body_lines = [
        f"Severity: {severity} ({len(reasons)} condition{'s' if len(reasons) != 1 else ''} triggered)",
        "",
        "Triggered conditions:",
    ]
    for r in reasons:
        body_lines.append(f"  - {r}")
    body_lines += [
        "",
        "Today's metrics:",
        f"  Total measurements: {today_data.get('total_measurements', '?')}",
        f"  Reach rate: {today_data.get('reach_rate', '?')}",
        f"  Fail rate: {today_data.get('fail_rate', '?')}",
    ]
    if "hourly_volume" in result:
        hv = result["hourly_volume"]
        body_lines.append(f"  Hourly volume: {hv['today']} (baseline median: {hv['baseline_median']})")
    if "type12" in result:
        t12 = result["type12"]
        body_lines.append(f"  Interdomain assumption: {t12['today']} (baseline median: {t12['baseline_median']})")
    body_lines += [
        "",
        "Baseline medians:",
        f"  Total measurements: {baseline_data.get('total_measurements', '?')}",
        f"  Reach rate: {baseline_data.get('reach_rate', '?')}",
        f"  Fail rate: {baseline_data.get('fail_rate', '?')}",
        "",
        f"Dashboard: {DASHBOARD_URL}" if DASHBOARD_URL else "",
    ]

    subject = f"[{severity}] revTr health alert — {date.today().isoformat()}"
    body = "\n".join(body_lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ALERT_EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        log.info("Alert email sent to %s", ALERT_EMAIL_TO)
    except Exception as e:
        log.error("Failed to send alert email: %s", e)


# Track last alert to avoid spamming (at most one email per hour)
_last_alert_time: datetime | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/health")
def api_health():
    """Return today's health metrics + baseline + daily breakdown."""
    global _last_alert_time
    target = date.today()
    current_hour = datetime.now(timezone.utc).hour

    df = fetch_daily_health(target, BASELINE_DAYS)
    hourly_df = fetch_hourly_health(target, BASELINE_DAYS, current_hour)
    hop_df = fetch_hop_quality(target, BASELINE_DAYS)

    result = evaluate_health(df, target, hourly_df=hourly_df, hop_df=hop_df)

    # Daily series over the selected range, sourced from the rollup table.
    range_str = request.args.get("range", "7d")
    start, end = _range_to_window(range_str, target)
    summary = fetch_summary(start, end)
    gran = _granularity_for_range(range_str)
    daily = []
    for label, s in _bucket_sums(
        summary, gran, ["total_measurements", "reaches_count", "failed_count"]
    ):
        total = s["total_measurements"]
        reaches = s["reaches_count"]
        failed = s["failed_count"]
        daily.append({
            "day": label,
            "total_measurements": total,
            "reaches_count": reaches,
            "failed_count": failed,
            "reach_rate": round(reaches / max(total, 1), 4),
            "fail_rate": round(failed / max(total, 1), 4),
        })
    result["daily"] = daily
    result["range"] = range_str
    result["granularity"] = gran
    result["current_hour_utc"] = current_hour

    # Send alert email (at most once per hour)
    if result["triggered"]:
        now = datetime.now(timezone.utc)
        if _last_alert_time is None or (now - _last_alert_time).total_seconds() > 3600:
            send_alert_email(result)
            _last_alert_time = now

    return jsonify(result)


@app.route("/api/failures")
def api_failures():
    """Failure-reason breakdown over the range, bucketed into primary categories."""
    range_str = request.args.get("range", "7d")
    start, end = _range_to_window(range_str, date.today())
    gran = _granularity_for_range(range_str)
    summary = fetch_summary(start, end)
    buckets: dict[str, dict] = {}
    order: list[str] = []
    totals: dict[str, int] = {}
    for _, row in summary.iterrows():
        label = _bucket_label(row["day"], gran)
        if label not in buckets:
            buckets[label] = {"failed_count": 0, "reasons": {}}
            order.append(label)
        b = buckets[label]
        b["failed_count"] += int(row["failed_count"])
        # fail_reasons is a numpy.ndarray of dicts (or None/NaN on zero-failure
        # days). Avoid `arr or []` — an ndarray's truth value is ambiguous.
        fr = row["fail_reasons"]
        items = [] if fr is None or isinstance(fr, float) else list(fr)
        for it in items:
            cat = _categorize_failure(it["reason"])
            cnt = int(it["cnt"])
            b["reasons"][cat] = b["reasons"].get(cat, 0) + cnt
            totals[cat] = totals.get(cat, 0) + cnt
    series = [{"day": lbl, "failed_count": buckets[lbl]["failed_count"],
               "reasons": buckets[lbl]["reasons"]} for lbl in order]
    return jsonify({"range": range_str, "granularity": gran, "series": series, "totals": totals})


@app.route("/api/hops")
def api_hops():
    """Per-hop-type composition fractions over the selected range (bucketed)."""
    range_str = request.args.get("range", "7d")
    start, end = _range_to_window(range_str, date.today())
    gran = _granularity_for_range(range_str)
    summary = fetch_summary(start, end)
    cols = ["total_hops", "measured_hops", "assumed_hops", "suspect_rr_atlas_count",
            *HOP_TYPE_COLUMNS.values()]
    series = []
    for label, s in _bucket_sums(summary, gran, cols):
        # Skip periods with no hop data (e.g. a revtr outage gap, or days whose
        # raw data has aged out) — otherwise total=0 renders as 100% "Other".
        if s["total_hops"] <= 0:
            continue
        total = s["total_hops"]
        point = {
            "day": label,
            "total_hops": s["total_hops"],
            "frac_measured": round(s["measured_hops"] / total, 4),
            "frac_assumed": round(s["assumed_hops"] / total, 4),
            "suspect_rr_atlas_count": s["suspect_rr_atlas_count"],
        }
        # Per-hop-type fractions (each type's share of all hops in the bucket).
        for key, col in HOP_TYPE_COLUMNS.items():
            point[f"frac_{key}"] = round(s[col] / total, 4)
        # Remainder so a stacked composition sums to 100% (hops not in any of the
        # classified type columns — e.g. unresponsive/unclassified hops).
        typed = sum(point[f"frac_{k}"] for k in HOP_TYPE_COLUMNS)
        point["frac_other"] = round(max(0.0, 1.0 - typed), 4)
        series.append(point)
    labels = {**HOP_TYPE_LABELS, "other": "Other / unclassified"}
    return jsonify({"range": range_str, "granularity": gran,
                    "labels": labels, "series": series})


@app.route("/api/rr_responsiveness")
def api_rr_responsiveness():
    """Record-Route responsiveness (responsive/probed targets) over the range (bucketed)."""
    range_str = request.args.get("range", "7d")
    start, end = _range_to_window(range_str, date.today())
    gran = _granularity_for_range(range_str)
    summary = fetch_summary(start, end)
    series = []
    for label, s in _bucket_sums(summary, gran, ["rr_probed_targets", "rr_responsive_targets"]):
        probed = s["rr_probed_targets"]
        responsive = s["rr_responsive_targets"]
        series.append({
            "day": label,
            "probed": probed,
            "responsive": responsive,
            "frac_responsive": round(responsive / max(probed, 1), 4),
        })
    return jsonify({"range": range_str, "granularity": gran, "series": series})


@app.route("/api/hop_quality")
def api_hop_quality():
    """Return daily interdomain / type-12 / suspect RR-atlas fractions."""
    target = date.today()
    df = fetch_hop_quality(target, BASELINE_DAYS)
    daily = []
    for _, row in df.iterrows():
        daily.append({
            "day": row["day"].isoformat(),
            "total_reaching": int(row["total_reaching"]),
            "interdomain_count": int(row["interdomain_count"]),
            "type12_count": int(row["type12_count"]),
            "suspect_rr_atlas_count": int(row["suspect_rr_atlas_count"]),
            "frac_interdomain": round(float(row["frac_interdomain"]), 4),
            "frac_type12": round(float(row["frac_type12"]), 4),
            "frac_suspect_rr_atlas": round(float(row["frac_suspect_rr_atlas"]), 4),
        })
    return jsonify({"daily": daily})


# Best-effort in-process cache of the precomputed-table read (per gunicorn
# worker). The table itself changes at most once/day, so a stale read costs
# nothing but a slightly old snapshot.
_as_dist_cache: dict[str, tuple[datetime, dict]] = {}


@app.route("/api/as_distribution")
def api_as_distribution():
    """Per-destination-AS-per-day distribution (precomputed daily; see cron/).

    Reads the small ``as_distribution_7d`` table and caches it in-process for
    AS_DIST_CACHE_TTL seconds.
    """
    now = datetime.now(timezone.utc)
    cached = _as_dist_cache.get("snapshot")
    if cached is not None and (now - cached[0]).total_seconds() < AS_DIST_CACHE_TTL:
        payload = dict(cached[1])
        payload["cached"] = True
        return jsonify(payload)

    try:
        df = fetch_as_distribution()
    except Exception as e:
        # The table is rebuilt daily via CREATE OR REPLACE (brief drop/recreate
        # window); don't 500 the panel if we read mid-rebuild — degrade to an
        # empty, retryable state.
        log.warning("as_distribution read failed: %s", e)
        return jsonify({
            "window": {"start": None, "end": None, "days": 0},
            "computed_at": None, "row_count": 0, "rows": [], "cached": False,
            "error": "AS distribution table is refreshing — try again shortly.",
        })
    rows = []
    days: set[str] = set()
    computed_at = None
    for _, r in df.iterrows():
        tests = _safe_int(r["tests"])
        reaches = _safe_int(r["reaches"])
        inter = _safe_int(r["interdomain_count"])
        name = r["as_name"]
        day = str(r["day"])
        days.add(day)
        if computed_at is None and "computed_at" in df.columns:
            computed_at = str(r["computed_at"])
        rows.append({
            "asn": r["asn"],
            "as_name": None if name is None or (isinstance(name, float) and pd.isna(name)) else str(name),
            "is_gcp": bool(r["is_gcp"]),
            "day": day,
            "unique_ips": _safe_int(r["unique_ips"]),
            "tests": tests,
            "reaches": reaches,
            "frac_reached": round(reaches / max(tests, 1), 4),
            "interdomain_count": inter,
            "frac_interdomain": round(inter / max(reaches, 1), 4),
        })
    days_sorted = sorted(days)
    payload = {
        "window": {"start": days_sorted[0] if days_sorted else None,
                   "end": days_sorted[-1] if days_sorted else None,
                   "days": len(days_sorted)},
        "computed_at": computed_at,
        "row_count": len(rows),
        "rows": rows,
        "cached": False,
    }
    _as_dist_cache["snapshot"] = (now, payload)
    return jsonify(payload)


@app.route("/api/ping")
def api_ping():
    """Check if the revTr API is alive by hitting /sources."""
    try:
        r = requests.get(
            f"{REVTR_BASE_URL}/sources",
            headers={"Revtr-Key": REVTR_API_KEY},
            timeout=8,
            verify=False,
        )
        r.raise_for_status()
        sources = r.json().get("srcs", [])
        return jsonify({"alive": True, "sources_count": len(sources)})
    except Exception as e:
        return jsonify({"alive": False, "error": str(e)})


@app.route("/api/queries_today")
def api_queries_today():
    """Return the number of revTr queries recorded today."""
    count = fetch_queries_today(date.today())
    return jsonify({"date": date.today().isoformat(), "count": count})


SITES_JSON_URL = "https://siteinfo.mlab-oti.measurementlab.net/v2/sites/sites.json"

_sites_cache: dict | None = None


def _load_sites() -> list[dict]:
    """Fetch and cache the M-Lab sites.json (prefix -> site metadata)."""
    global _sites_cache
    if _sites_cache is not None:
        return _sites_cache
    r = requests.get(SITES_JSON_URL, timeout=15)
    r.raise_for_status()
    _sites_cache = r.json()
    return _sites_cache


def _match_ip_to_site(ip_str: str, sites: list[dict]) -> dict | None:
    """Match an IP address to an M-Lab site via IPv4 prefix."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    for site in sites:
        prefix = (site.get("network") or {}).get("ipv4", {}).get("prefix")
        if not prefix:
            continue
        try:
            if addr in ipaddress.ip_network(prefix, strict=False):
                return site
        except ValueError:
            continue
    return None


@app.route("/api/sites")
def api_sites():
    """Return per-M-Lab-site measurement counts with geolocation for today."""
    target = date.today()
    sites_meta = _load_sites()

    # Get per-src counts from BQ (physical sites only)
    query = f"""
    SELECT
      t.raw.src AS vp_ip,
      COUNT(*) AS cnt
    FROM `measurement-lab.revtr_raw.revtr1` t
    WHERE DATE(t.date) = DATE('{target.isoformat()}')
      AND NOT (NET.IP_FROM_STRING(t.raw.dst) BETWEEN
               NET.IP_FROM_STRING('34.0.0.0') AND NET.IP_FROM_STRING('34.255.255.255'))
    GROUP BY vp_ip
    """
    df = run_query(query)

    # Match each VP IP to an M-Lab site and aggregate per site
    site_counts: dict[str, dict] = {}
    unmatched = 0
    for _, row in df.iterrows():
        site = _match_ip_to_site(row["vp_ip"], sites_meta)
        if site is None:
            unmatched += int(row["cnt"])
            continue
        name = site["name"]
        if name not in site_counts:
            loc = site.get("location", {})
            site_counts[name] = {
                "site": name,
                "city": loc.get("city", ""),
                "country": loc.get("country_code", ""),
                "lat": loc.get("latitude"),
                "lng": loc.get("longitude"),
                "type": (site.get("annotations") or {}).get("type", ""),
                "count": 0,
            }
        site_counts[name]["count"] += int(row["cnt"])

    result = sorted(site_counts.values(), key=lambda s: s["count"], reverse=True)
    return jsonify({"date": target.isoformat(), "sites": result, "unmatched": unmatched})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="revTr Health Dashboard")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5050")))
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # Suppress InsecureRequestWarning for the revTr API (self-signed cert)
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"Starting revTr Health Dashboard on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
