# revTr timeout spike since 2026-06-14

**TL;DR.** revTr *timeout* failures jumped ~10–15× starting **June 14**
(~20–40k/day → ~360–530k/day, still climbing). The clear trigger is **AS16591
(Google Fiber)**: its reverse-path reachability collapsed (~51% → ~0.6%) and the
same ~400 targets then got hammered with retries (~4 → ~950 probes/IP/day) —
~344k failed probes on the 14th alone (~87% of that day's timeout surge). From
the 15th the volume spreads: **Comcast** also shows a genuine reach drop
(61% → 35%), while several networks that are *chronically* near-unreachable via
revTr (Eircom, AT&T, Starlink) get probed in far higher volume — adding timeouts
without any reachability change. gaplimit (the usual dominant failure mode) is
unchanged — the increase is **entirely timeouts**.

## 1. The signal — it's specifically timeouts

Daily revTr failures by category (M-Lab, GCP `34.0.0.0/8` excluded):

| day | **timed out** | gaplimit | total failures |
|-----|--------------:|---------:|---------------:|
| Jun 9–13 (typical) | 18k–44k | ~0.6–1.2M | 0.7–1.26M |
| **Jun 14** | **396,554** | 977,190 | 1,390,359 |
| **Jun 15** | **361,737** | 1,145,998 | 1,529,854 |
| **Jun 16** | **531,774** | 1,044,255 | 1,595,977 |

gaplimit is flat; the whole increase is the **timed-out** bucket.

## 2. Google Fiber (AS16591) — the trigger, via a retry storm

| period | probes | unique target IPs | reach | probes/IP |
|--------|-------:|------------------:|------:|----------:|
| Jun 9–13 | ~1.5–2.8k/day | ~400 | ~53% | ~4 |
| **Jun 14** | **345,366** | 364 | **0.3%** | **949** |
| Jun 15 | 103,688 | 467 | 1.5% | 222 |

The **same ~400** Google Fiber targets revTr always probes stopped responding
around June 14 (the unique-IP count barely moved). What exploded is **attempts
per IP** (~4 → ~950/day): unanswered probes time out and are retried hard,
multiplying the timeout count.

## 3. Biggest reverse-path reachability drops (Jun 9–13 → Jun 14–15)

| AS | network | reach before | reach after |
|----|---------|-------------:|------------:|
| **16591** | **Google Fiber** | **50.7%** | **0.6%** |
| 51852 | Private Layer | 87.7% | 62.9% |
| 9158 | Telenor | 51.9% | 30.8% |
| 7922 | Comcast | 61.6% | 41.1% |
| 63949 | Linode | 82.0% | 66.2% |

### Jun 15 — genuine reach drops vs. higher probing of already-unreachable networks

By the 15th the top non-reaching networks split into two groups. Only **Comcast**
and **Google Fiber** actually *lost* reachability; the rest were already
near-unreachable before the 14th and simply got probed much more:

| AS | network | reach Jun 9–13 | reach Jun 15 | non-reaching Jun 15 | genuine drop? |
|----|---------|---------------:|-------------:|--------------------:|---------------|
| 7922  | Comcast      | 61.4% | 34.5% | 107,471 | **yes** (−27 pts) |
| 16591 | Google Fiber | 50.5% |  1.5% | 102,153 | **yes** (−49 pts) |
| 5466  | Eircom       |  6.7% |  0.6% |  72,451 | no — already near-0% |
| 7018  | AT&T         |  9.3% |  6.9% |  61,439 | no — already low |
| 60855 | (no name)    |  0.0% |  0.0% |  56,708 | no — always 0% |
| 14593 | Starlink     |  1.2% |  1.4% |  54,357 | no — always ~1% |

Eircom / AT&T / Starlink / AS60855 add timeouts because their **probe volume rose**
(e.g. Eircom ~6k → ~38k probes/day), not because anything changed on their side.
So the genuine reachability *changes* are confined to Comcast and Google Fiber
(plus Private Layer / Telenor / Linode from the table above); the remaining
timeout volume is increased probing of chronically-unreachable targets.

## 4. Mechanism

target stops responding → probe gets no reply → **timeout** → aggressive retry
→ timeout volume balloons. A reachability change on a single AS is amplified
~250× in failure volume by the retry behavior.

## 5. Open questions

- **Why did ~400 Google Fiber (AS16591) targets go dark on June 14?** Same target
  IPs as before — target-side change, a route/VP change, or a campaign change?
- The 15th–16th spread across many unrelated networks at near-0% reach raises the
  question of a **system / VP / source-side** change vs. independent target
  outages. Worth checking whether other VPs see the same drop.
- Should retry/backoff be **capped for persistently-unreachable targets** so one
  reachability change can't amplify into hundreds of thousands of timeouts?

---
*Method: failure categories from the revTr health rollup (`daily_summary.fail_reasons`,
sourced from `measurement-lab.revtr_raw`); per-AS reach from destination-IP→AS
longest-prefix match (hopannotation2 + RouteViews `pfx2as`), reach =
`stop_reason='REACHES'` / probes. GCP client range (`34.0.0.0/8`) excluded
throughout. Note: the per-AS table carries reach/volume but not `fail_reason`, so
the aggregate timeout spike (§1) is measured directly, while attributing it to
specific networks (§2–3) is inferred from their reach collapse + volume — a
per-AS `fail_reason` breakdown from `revtr_raw` would confirm it.*
