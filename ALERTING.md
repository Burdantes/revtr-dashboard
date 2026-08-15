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

~1.33 GiB scanned per evaluation (3 queries against
`measurement-lab.revtr_raw.revtr1`), billed to `measurement-lab`. Hourly is
~0.031 TiB/day.

## Known limitation

`revtr_raw.revtr1` has gaps — including a complete 6-day outage,
2026-08-07..12. A "7-day" baseline over a window containing a gap is really a
1–2 day median. Alert bodies report the number of days **actually present**
rather than claiming 7, but be aware the baseline can be thin, which makes the
thresholds noisier than they look.
