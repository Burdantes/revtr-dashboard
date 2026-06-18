# revTr timeout spike since 2026-06-14

**TL;DR.** revTr *timeout* failures jumped ~10–15× starting **June 14** and have
held through the 16th (~20–40k/day → ~400–530k/day). Overall measurement volume
is roughly **flat** (~2.5M/day) — so this is a reachability collapse + probe
*concentration*, not a volume increase. Two networks drive it:
**AS16591 (Google Fiber)** — reach collapsed ~51% → ~0.3% and the same ~400
targets get hammered with more measurements (~4 → ~700–950 probes/IP/day), **persisting
across the 14th–16th**; and **AS7922 (Comcast)** — a reach drop (61% → 33%) plus a
volume surge (74k → 344k probes/day) that **ramps over the 15th–16th**, making it
the single largest contributor by the 16th. Networks like Starlink and AT&T look
bad but are *chronically* near-unreachable and flat — not part of the change.
gaplimit (the usual dominant failure mode) is unchanged — the increase is
**entirely timeouts**.

## 1. The signal is specifically timeouts

Daily revTr failures by category (M-Lab, GCP `34.0.0.0/8` excluded). Total
measurements stay ~flat while reaches fall and timeouts spike:

| day | total meas. | reaches | **timed out** | gaplimit | total failures |
|-----|------------:|--------:|--------------:|---------:|---------------:|
| Jun 10–13 (avg) | ~2,518,000 | ~1,322,000 | ~31,000 | ~1,144,000 | ~1,196,000 |
| **Jun 14** | 2,394,865 | 1,004,506 | **396,554** | 977,190 | 1,390,359 |
| **Jun 15** | 2,763,246 | 1,233,392 | **361,737** | 1,145,998 | 1,529,854 |
| **Jun 16** | 2,727,153 | 1,131,176 | **531,774** | 1,044,255 | 1,595,977 |

Same total probing, ~15–25% fewer reaches, and the entire failure rise is the
**timed-out** bucket (gaplimit, the usual dominant mode, is flat).

## 2. Google Fiber (AS16591) — reach collapse + retry storm, persists 14–16

| day | probes | unique target IPs | reach | probes/IP |
|-----|-------:|------------------:|------:|----------:|
| Jun 10–13 (avg) | ~1,940/day | ~430 | ~50% | ~4 |
| **Jun 14** | **345,366** | 364 | **0.3%** | **949** |
| Jun 15 | 103,688 | 467 | 1.5% | 222 |
| **Jun 16** | **287,498** | 422 | **0.3%** | **681** |

The **same ~400** target IPs revTr always probes stopped responding around June 14
(unique-IP count barely moves); what exploded is **probes per IP** (~4 →
~700–950/day). On the 14th alone that's ~344k failed probes ≈ **87% of that day's
timeout surge**. 

## 3. Does it hold across days? Per-AS reach + volume, Jun 14–16

For each network: reach and probes/day, **baseline (Jun 10–13 avg) vs each day**.
This separates genuine reachability *changes* from networks that are simply
chronically unreachable.

**Reach (% of probes that reach):**

| AS | network | base | Jun 14 | Jun 15 | Jun 16 | verdict |
|----|---------|-----:|-------:|-------:|-------:|---------|
| 16591 | Google Fiber | 50% | 0.3% | 1.5% | 0.3% | **collapse, persists** |
| 7922  | Comcast      | 61% | 60%  | 35%  | 33%  | **drop from the 15th onwards** |
| 14593 | Starlink     | 1.2% | 1.2% | 1.4% | 1.2% | flat (chronically low) |
| 7018  | AT&T         | 9%  | 8%   | 7%   | 9%   | flat (chronically low) |
| 5466  | Eircom       | 7%  | 7%   | 0.6% | 8%   | 1-day dip (15th only) |

**Probes/day (baseline avg → each day):**

| AS | network | base avg | Jun 14 | Jun 15 | Jun 16 | verdict |
|----|---------|---------:|-------:|-------:|-------:|---------|
| 16591 | Google Fiber | ~1,940  | 345,366 | 103,688 | 287,498 | huge (retry storm) |
| 7922  | Comcast      | ~73,900 |  58,923 | 163,994 | **343,878** | **surges, ramps 15→16** |
| 14593 | Starlink     | ~56,100 |  50,050 |  55,128 |  52,324 | flat |
| 7018  | AT&T         | ~45,600 |  35,623 |  66,020 |  44,008 | ~flat |
| 5466  | Eircom       |  ~6,700 |   4,293 |  72,865 |   6,008 | 1-day spike (15th) |

**Takeaways:**
- **Google Fiber** and **Comcast** are the only two noticeable events, and both
  persist/grow through the 16th. By the 16th they are the two largest
  non-reaching contributors (Google Fiber ~287k, Comcast ~229k).
- **Starlink, AT&T** are consistently near-unreachable; they inflate
  raw failure counts but did not change; they are not part of the spike.
- **Eircom** was a single-day (Jun 15) spike in both volume and failure, already
  back to normal by the 16th.

Top non-reaching networks on **Jun 16**: Google Fiber 286,667 · Comcast 229,130 ·
Amazon 64,486 · Starlink 51,670 · AT&T 39,944 · BT 35,215 — Google Fiber +
Comcast alone are ~516k of the day's non-reaching.

## 4. Mechanism

The overall probe budget is ~flat, so retries to the
failing networks (Google Fiber, then Comcast) **concentrate** the budget onto
timeouts and pull it away from other targets - a reachability change on one or two
ASes is amplified into hundreds of thousands of timeouts.

---
*Method: failure categories from the revTr health rollup (`daily_summary.fail_reasons`,
sourced from `measurement-lab.revtr_raw`); per-AS reach/volume from destination-IP→AS
longest-prefix match (hopannotation2 + RouteViews `pfx2as`), reach =
`stop_reason='REACHES'` / probes. GCP client range (`34.0.0.0/8`) excluded
throughout. Baseline is **Jun 10–13** (the per-AS table is a rolling 7-day window;
Jun 9 has aged out). The aggregate timeout spike (§1) is measured directly; the
per-AS table has reach/volume but not `fail_reason`, so the per-network attribution
(§2–3) is inferred from reach collapse + volume — a per-AS `fail_reason` breakdown
from `revtr_raw` would confirm it.*
