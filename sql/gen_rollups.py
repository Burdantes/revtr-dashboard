#!/usr/bin/env python3
"""Regenerate 03_backfill.sql and 04_scheduled_rollup.sql from 02_rollup_daily.sql.

02 is the single source of truth for the rollup MERGE. 03 and 04 are the same
MERGE wrapped in a per-day WHILE loop (date range vs trailing 3 days), with the
MERGE body indented by 2 spaces. These were historically hand-kept-in-sync,
which let them drift; run this after editing 02 to keep all three identical.

Usage: python sql/gen_rollups.py   (writes 03 and 04 in place)
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "02_rollup_daily.sql"
MERGE_START = "MERGE `nsf-2148275-66720.revtr_dashboard.daily_summary` T"

LOOP_HEADER_03 = """\
-- Backfill daily_summary over [start_date, end_date]. Idempotent per day.
-- GENERATED from 02_rollup_daily.sql by gen_rollups.py (do not edit by hand).
-- Edit the two dates below before running. Each day scans ~0.9 GB.
DECLARE start_date DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY);
DECLARE end_date   DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY);
DECLARE target DATE;

SET target = start_date;
WHILE target <= end_date DO
"""

LOOP_HEADER_04 = """\
-- Scheduled daily rollup: re-rolls the last 3 days (idempotent) so a missed
-- run self-heals. Install as a BigQuery Scheduled Query running as an identity
-- with measurement-lab read + nsf-2148275-66720 write. Daily cadence.
-- GENERATED from 02_rollup_daily.sql by gen_rollups.py (do not edit by hand).
DECLARE start_date DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY);
DECLARE end_date   DATE DEFAULT DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY);
DECLARE target DATE;

SET target = start_date;
WHILE target <= end_date DO
"""

LOOP_FOOTER = """\

  SET target = DATE_ADD(target, INTERVAL 1 DAY);
END WHILE;
"""


def merge_body() -> str:
    text = SRC.read_text()
    idx = text.index(MERGE_START)
    body = text[idx:].rstrip("\n")
    # Indent every non-empty line by 2 spaces to match the WHILE-loop nesting.
    return "\n".join((("  " + ln) if ln.strip() else ln) for ln in body.splitlines())


def main() -> None:
    body = merge_body()
    (HERE / "03_backfill.sql").write_text(LOOP_HEADER_03 + body + "\n" + LOOP_FOOTER)
    (HERE / "04_scheduled_rollup.sql").write_text(LOOP_HEADER_04 + body + "\n" + LOOP_FOOTER)
    print("regenerated 03_backfill.sql and 04_scheduled_rollup.sql from 02_rollup_daily.sql")


if __name__ == "__main__":
    main()
