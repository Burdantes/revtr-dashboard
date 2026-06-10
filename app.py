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
from flask import Flask, jsonify, render_template
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

# Alert thresholds (same defaults as revtr_health_alert.py)
VOLUME_DROP_RATIO = 0.5
QUALITY_DROP_RATIO = 0.75
QUALITY_DROP_ABS = 0.1
FAIL_RATE_INCREASE_ABS = 0.1

ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "ls3748@columbia.edu")
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
    """Fraction of reaching measurements with interdomain symmetry / fishy type 4."""
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
        EXISTS(
          SELECT 1
          FROM UNNEST(t.raw.revtr_hops) h
          WHERE h.hop_type = 4
            AND h.rtt > IFNULL((
              SELECT MAX(h2.rtt)
              FROM UNNEST(t.raw.revtr_hops) h2
              WHERE h2.hop_type != 4 AND h2.rtt IS NOT NULL
            ), 0) + 50
        ) AS has_fishy_type4
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
      COUNTIF(has_fishy_type4) AS fishy_type4_count
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
    df["frac_fishy_type4"] = df["fishy_type4_count"] / df["total_reaching"].clip(lower=1)
    return df


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
        "Dashboard: http://136.116.232.100:5050",
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

    # Daily rows for chart/table
    daily = []
    for _, row in df.iterrows():
        daily.append({
            "day": row["day"].isoformat(),
            "total_measurements": int(row["total_measurements"]),
            "reaches_count": int(row["reaches_count"]),
            "failed_count": int(row["failed_count"]),
            "reach_rate": round(float(row["reach_rate"]), 4),
            "fail_rate": round(float(row["fail_rate"]), 4),
        })
    result["daily"] = daily
    result["current_hour_utc"] = current_hour

    # Send alert email (at most once per hour)
    if result["triggered"]:
        now = datetime.now(timezone.utc)
        if _last_alert_time is None or (now - _last_alert_time).total_seconds() > 3600:
            send_alert_email(result)
            _last_alert_time = now

    return jsonify(result)


@app.route("/api/hop_quality")
def api_hop_quality():
    """Return daily interdomain / type-12 / fishy-type-4 fractions."""
    target = date.today()
    df = fetch_hop_quality(target, BASELINE_DAYS)
    daily = []
    for _, row in df.iterrows():
        daily.append({
            "day": row["day"].isoformat(),
            "total_reaching": int(row["total_reaching"]),
            "interdomain_count": int(row["interdomain_count"]),
            "type12_count": int(row["type12_count"]),
            "fishy_type4_count": int(row["fishy_type4_count"]),
            "frac_interdomain": round(float(row["frac_interdomain"]), 4),
            "frac_type12": round(float(row["frac_type12"]), 4),
            "frac_fishy_type4": round(float(row["frac_fishy_type4"]), 4),
        })
    return jsonify({"daily": daily})


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
