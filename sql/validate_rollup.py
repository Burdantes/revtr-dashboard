"""Validate a daily_summary row against direct aggregates over the raw views.

Usage: python validate_rollup.py 2026-06-09
Exits non-zero on mismatch.
"""
import sys
from google.cloud import bigquery

PROJECT = "nsf-2148275-66720"
SUMMARY = f"{PROJECT}.revtr_dashboard.daily_summary"

GCP = ("NOT (NET.IP_FROM_STRING(r.raw.dst) BETWEEN "
       "NET.IP_FROM_STRING('34.0.0.0') AND NET.IP_FROM_STRING('34.255.255.255'))")


def main(day: str) -> int:
    client = bigquery.Client(project=PROJECT)

    summ = list(client.query(
        f"SELECT total_measurements, reaches_count, failed_count "
        f"FROM `{SUMMARY}` WHERE day = DATE('{day}')").result())
    if not summ:
        print(f"FAIL: no daily_summary row for {day}")
        return 1
    s = summ[0]

    raw = list(client.query(f"""
        SELECT COUNT(*) AS total,
               COUNTIF(r.raw.stop_reason = 'REACHES') AS reaches,
               COUNTIF(r.raw.fail_reason IS NOT NULL AND r.raw.stop_reason != 'REACHES') AS failed
        FROM `measurement-lab.revtr_raw.revtr1` r
        WHERE r.date = DATE('{day}') AND {GCP}""").result())[0]

    ok = True
    for name, a, b in [("total_measurements", s["total_measurements"], raw["total"]),
                       ("reaches_count", s["reaches_count"], raw["reaches"]),
                       ("failed_count", s["failed_count"], raw["failed"])]:
        status = "ok" if a == b else "MISMATCH"
        if a != b:
            ok = False
        print(f"{name}: summary={a} raw={b} [{status}]")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
