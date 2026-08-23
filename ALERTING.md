# revTr anomaly email alerting

Emails you when revTr health degrades, without depending on anyone looking at
the dashboard.

## How it fits together

| Piece | Role |
|---|---|
| `alerting.py` | Severity classification, formatting, SMTP delivery, dedup. The only place any of that lives. |
| `app.py` → `/api/health` | Evaluates health for the dashboard. Opportunistically alerts on page load. |
| `cron/alert_runner.py` | Hourly evaluation. **This is what actually guarantees delivery.** Imports the fetch/evaluate functions from `app.py` rather than reimplementing them, so cron and dashboard can never disagree about a threshold. |
| `cron/run_alert_check.sh` | One-off-container wrapper, same pattern as `run_rollup.sh`. |
| `~/revtr-alerts/alert.env` | Credentials + tuning. chmod 600, outside the repo (this repo is public). See `alert.env.example`. |
| `~/revtr-alerts/state.json` | Dedup state, mounted into both the dashboard and cron containers so they cannot double-send. |

## Severity

Conditions checked: volume drop (hour-adjusted), reach-rate drop, reach-rate
spike, failure-rate spike, interdomain-assumption spike.

| Severity | When | Emails? |
|---|---|---|
| `ok` | nothing triggered | no |
| `warning` | 1 condition | no (dashboard only) |
| `high` | 2 conditions | yes |
| `critical` | 3+ conditions, **or any hard failure** | yes |

**Hard failures** are promoted to `critical` regardless of how many conditions
fired: no data at all for the day, or volume below 20% of baseline. This exists
because severity used to be a pure count, which meant a total outage — one
condition — ranked *below* three mild threshold wobbles and would never have
emailed.

## Dedup

- An unchanged condition set re-sends after `ALERT_REPEAT_HOURS` (default 6).
- **Escalation bypasses the window.** `high` → `critical` pages immediately.
- A *different* condition set is treated as new information and sends.
- Dedup keys on the condition name, not the whole message, because the message
  embeds live metric values that drift every run.
- State advances **only after a successful send**, so a failed SMTP call retries
  on the next run instead of silently consuming the window.

## Heartbeat

`--heartbeat` sends a scheduled all-clear (Mondays) even when nothing is wrong.
An alerting system that correctly sends nothing for months is indistinguishable
from one that broke silently. If the Monday mail stops arriving, the channel is
down. It never writes dedup state, so it cannot suppress a real alert.

## Operating it

```bash
# Verify delivery end to end, no BigQuery, no waiting for an outage:
~/revtr-rollup/run_alert_check.sh --test-email

# What would it say right now?
~/revtr-rollup/run_alert_check.sh --dry-run

# Same, for the weekly digest:
~/revtr-rollup/run_alert_check.sh --dry-run --heartbeat

# Force a re-send (clears the quiet window):
rm ~/revtr-alerts/state.json
```

## Troubleshooting delivery

**Never `source` or `.` `alert.env` in bash.** SMTP passwords routinely contain
`)`, `%`, `,`, `@` and other shell metacharacters, so sourcing it either throws
a syntax error or silently mangles the value — and it echoes the password into
your terminal and scrollback. Docker's `--env-file` takes values **literally**
with no shell parsing, which is why the container authenticates fine even when
sourcing the same file fails. Always go through `--env-file`.

**"Sent" is not "delivered."** `send_email()` returning True means the relay
returned 250. Use `smtp_probe.py` to see the server's final response and any
queue id:

```bash
docker run --rm --env-file $HOME/revtr-alerts/alert.env \
  -v $HOME/revtr-alerts/smtp_probe.py:/probe.py:ro \
  --entrypoint python revtr-monitor:latest /probe.py
```

**SES Mail Manager vs standard SES.** These are different products:

| | Endpoint | Username | Sends without extra config? |
|---|---|---|---|
| Standard SES | `email-smtp.<region>.amazonaws.com` | `AKIA…` (20 ch) | yes |
| Mail Manager ingress | `<id>.fips.<x>.mail-manager-smtp.amazonaws.com` | `inp-…` | **no** — the rule set needs a "Send to Internet" action |

A Mail Manager ingress point accepts the message into a pipeline. Without a
"Send to Internet" rule action it is archived or dropped, and the SMTP
conversation still returns 250 either way.

**This deployment uses Mail Manager, and it works** — verified 2026-08-15 by
confirmed inbox delivery, not just a 250. Do not "fix" it by switching to the
standard SES endpoint. If you ever do rebuild it from scratch, standard SES is
the simpler starting point for single-recipient alert mail, but there is no
reason to migrate a working setup.

Either way the `SMTP_FROM` address must be a **verified identity in the same
region** as the endpoint. Note that a 250 at `RCPT TO` proves nothing about
routing: the ingress point accepts per the traffic policy and defers all
routing to the rule set, so it returns 250 for essentially any recipient. When
delivery is in doubt, the answer is in the Mail Manager rule set and archive in
the AWS console, not in the SMTP conversation.

## Deploying a change

The VM runs from `~/revtr-monitor-git`, a real clone. Images are tagged with the
commit sha so `docker ps` answers "what is live?".

```bash
cd ~/revtr-monitor-git && git fetch && git checkout <sha>
docker build -t revtr-monitor:<sha> -t revtr-monitor:latest .
# Cron reads from the /work mount, so the clone alone is not enough:
cp cron/alert_runner.py cron/run_alert_check.sh ~/revtr-rollup/
docker rm -f revtr-monitor && docker run -d --name revtr-monitor \
  --restart unless-stopped -p 5050:5050 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json -e PORT=5050 \
  -e BQ_PROJECT=measurement-lab -e REVTR_API_KEY=... \
  -e ALERT_STATE_PATH=/state/state.json \
  --env-file $HOME/revtr-alerts/alert.env \
  -v $HOME/adc.json:/creds/adc.json:ro -v $HOME/revtr-alerts:/state \
  revtr-monitor:<sha>
```

## Cost

~5.37 GiB scanned per evaluation (4 queries against
`measurement-lab.revtr_raw.revtr1`), billed to `measurement-lab`. Hourly is
~0.126 TiB/day, ~$24/month at on-demand list.

An earlier figure of 1.33 GiB was measured on 2026-08-15 while 6 of the 8
baseline days were empty from the Aug 7-12 outage. Those partitions have
backfilled; the queries did not change. **Any cost estimate measured over a data
gap is an underestimate** - check the window is populated before quoting one.

The freshness query is only 0.15 GiB of the total; the bulk is the 8-day
baseline window. Reading the baseline from `daily_summary` instead of raw would
cut it substantially and matches the precompute pattern already used for
`as_distribution_7d`.

## The partition day is not the calendar day

`revtr_raw.revtr1` partitions on a day running **04:00 UTC → 04:00 UTC**.
Partition `D` holds raw timestamps from `D 04:00` through `D+1 03:59`. So
between 00:00 and 04:00 UTC, `WHERE DATE(t.date) = CURRENT_DATE()` returns zero
rows because today's partition has not opened yet.

Read as "no data", that looks identical to a total outage. It generated **38
false criticals and 16 false alert emails in the first 8 days** — two every
night at 00:15 and 03:15 UTC, with every hour from 05:00 onward reporting `ok`.

Three rules follow:

- "Today" means the partition being written: `(now_utc − offset).date()`, via
  `app.partition_day()`. Not `date.today()`.
- Never truncate on hour-of-day for a today-vs-baseline comparison. A partition's
  raw hours run 04..23 then 00..03, so `EXTRACT(HOUR) <= current_hour` selects a
  ragged, non-comparable slice. Compare elapsed time from each row's *own*
  partition start.
- **`raw.date` runs ~30–55 min ahead of wall clock**, so
  `CURRENT_TIMESTAMP() − MAX(raw.date)` is normally negative. Only positive lag
  is evidence of staleness.

The offset is derived from the data at runtime and a mismatch against
`REVTR_PARTITION_OFFSET_HOURS` is logged, so an upstream change surfaces instead
of quietly bringing the false alarms back.

Do **not** try to detect the offset with `MIN(EXTRACT(HOUR FROM raw.date))`
grouped by partition — it returns 0 for a full partition (which contains hours
00–03 of the next calendar day) and so reads as proof of alignment. Use
`MIN(TIMESTAMP_DIFF(ts, TIMESTAMP(pday), HOUR))`.

## Known limitation

`revtr_raw.revtr1` has gaps — including a complete 6-day outage,
2026-08-07..12. A "7-day" baseline over a window containing a gap is really a
1–2 day median. Alert bodies report the number of days **actually present**
rather than claiming 7, but be aware the baseline can be thin, which makes the
thresholds noisier than they look.
