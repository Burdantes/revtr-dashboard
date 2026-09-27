"""Could a reverse traceroute be reused across NDT tests from the same pair,
and how often would the reused path be wrong?

Three arrival streams, keyed by the pair a measurement is keyed on:

  ndt      completed NDT7 tests,          key (server.Site, client /24 or /48)
  scamper  traceroute-caller, one row per TCP connection (cached or fresh),
                                          key (Tracelb.src, client /24 or /48)
  revtr    RevTr sidecar firings,         key (raw.src, raw.dst)  -- dst is already /24
           (label ndt_revtr_sidecar; the load a cache would remove)

For each stream it computes the within-pair inter-arrival distribution and,
for a grid of staleness thresholds T, the share of events that would NOT need
a new measurement under two cache policies:

  sliding   reuse if ANY event of the pair happened within T   (upper bound)
  fill      reuse if the last MEASURED result is younger than T (what a
            per-measurement staleness threshold actually implements)

The first event of every pair inside the window is counted as a miss, so hit
rates are slightly conservative for long T.

Path change (scamper and revtr only). Restricted to events that carry their
own freshly measured path (scamper: CachedResult = false; revtr: every firing
with hops), and compared only between measurements of the SAME HOST -- the
reuse key above is a /24, and different hosts of one /24 differ in path (and
even in origin AS) without any route change. Host = the exact client IP
(scamper Tracelb.dst; revtr the hop_type = 1 hop, since raw.dst is the /24
written as x.y.z.0). It reports

  path_change_by_gap.csv   consecutive measurements of a (server, host),
                           split by gap and by whether the signature changed;
                           changed_loop counts changes where either revtr
                           measurement has an AS loop (A>B>A) in its RR hops
  stale_by_threshold.csv   the fill policy re-run per (server, host) on that
                           subsequence, hits split into same-path and
                           changed-path serves

Signatures ignore hops inside the client /24 (/48):

  scamper  sig_ip     set of responsive Tracelb node addresses (MDA: a set,
                      not a sequence; partial discovery of load-balanced
                      interfaces shows up as change)
           sig_p24    set of /24s (/48s) of those addresses -- coarser
  revtr    sig_ip     hop IP sequence, all hop types
           sig_as     AS sequence of all hop types, consecutive dups collapsed
           sig_rr_ip  hop IP sequence of measured record-route hops only
                      (hop_type 5 RR, 6 spoofed RR)
           sig_rr_as  AS sequence of those hops -- the route-change measure;
                      the all-hop levels also move when revtr stitches the
                      path differently (type 3/4 intersections, 11/12 assumed)

Scamper timestamps. On CachedResult rows CycleStart, Tracelb.start and the
filename timestamp are all copied from the original trace, and parser.Time is
parse time. The connection time comes from tcpinfo raw.Metadata.StartTime,
joined on UUID (checked 2026-09-25, tgd01, 2026-09-20: 36,396/36,396 rows
matched; fresh traces start 0-16 s after the connection, cached ones reuse a
trace up to ~655 s old, i.e. traceroute-caller's own cache is ~10 min per
client IP). Consequence: two fresh scamper traces to the SAME client IP are
>= ~10 min apart, so short-gap scamper path pairs are different hosts of one /24.

Cost: ~10 GB for 3 partitions (scamper 8.7, revtr 1.0, ndt ~0.4), billed to
measurement-lab. Scamper is ~33M rows for 3 days; peak RAM a few GiB.
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from google.cloud import bigquery  # noqa: E402

BILLING_PROJECT = "measurement-lab"
THRESHOLDS_S = [60, 300, 600, 1800, 3600, 2 * 3600, 6 * 3600, 12 * 3600, 24 * 3600]
GAP_EDGES_S = [0, 1, 10, 60, 300, 600, 1800, 3600, 2 * 3600, 6 * 3600, 12 * 3600, 24 * 3600, np.inf]
STREAM_COLORS = {"ndt": "#2a78d6", "scamper": "#eb6834", "revtr": "#1baf7a"}
# level -> (linestyle, marker, alpha); RR-only revtr levels drawn full, all-hop ones faded
LEVEL_STYLES = {
    "sig_ip": ("-", "o", 0.45),
    "sig_p24": ("--", "o", 1.0),
    "sig_as": ("--", "o", 0.45),
    "sig_rr_ip": ("-", "s", 1.0),
    "sig_rr_as": ("--", "s", 1.0),
}

NDT_SQL = """
SELECT
  FARM_FINGERPRINT(CONCAT(server.Site, "|", NET.IP_TO_STRING(NET.IP_TRUNC(
    NET.IP_FROM_STRING(raw.ClientIP), IF(STRPOS(raw.ClientIP, ":") > 0, 48, 24))))) AS k,
  UNIX_SECONDS(a.TestTime) AS ts
FROM `measurement-lab.ndt.ndt7`
WHERE date BETWEEN @start AND @end
"""

SCAMPER_SQL = """
WITH s AS (
  SELECT
    raw.Metadata.UUID AS uuid,
    NOT raw.Metadata.CachedResult AS fresh,
    raw.Tracelb.src AS src,
    raw.Tracelb.dst AS dst,
    NET.IP_TRUNC(NET.SAFE_IP_FROM_STRING(raw.Tracelb.dst),
                 IF(STRPOS(raw.Tracelb.dst, ":") > 0, 48, 24)) AS dpfx,
    raw.Tracelb.nodes AS nodes
  FROM `measurement-lab.ndt.scamper1`
  WHERE date BETWEEN @start AND @end),
h AS (
  SELECT uuid, fresh, src, dst, dpfx,
    ARRAY(
      SELECT AS STRUCT addr, pfx FROM (
        SELECT DISTINCT n.addr,
          NET.IP_TO_STRING(NET.IP_TRUNC(NET.SAFE_IP_FROM_STRING(n.addr),
                                        IF(STRPOS(n.addr, ":") > 0, 48, 24))) AS pfx
        FROM UNNEST(nodes) n WHERE n.addr IS NOT NULL)
      WHERE pfx IS NOT NULL AND pfx != NET.IP_TO_STRING(dpfx)) AS hops
  FROM s WHERE dpfx IS NOT NULL),
t AS (
  SELECT raw.Metadata.UUID AS uuid, MIN(UNIX_SECONDS(raw.Metadata.StartTime)) AS ts
  FROM `measurement-lab.ndt.tcpinfo`
  WHERE date BETWEEN DATE_SUB(@start, INTERVAL 1 DAY) AND DATE_ADD(@end, INTERVAL 1 DAY)
  GROUP BY 1)
SELECT
  FARM_FINGERPRINT(CONCAT(h.src, "|", NET.IP_TO_STRING(h.dpfx))) AS k,
  FARM_FINGERPRINT(CONCAT(h.src, "|", h.dst)) AS pk,
  t.ts, h.fresh,
  IF(h.fresh, (SELECT FARM_FINGERPRINT(STRING_AGG(x.addr, "," ORDER BY x.addr))
               FROM UNNEST(h.hops) x), NULL) AS sig_ip,
  IF(h.fresh, (SELECT FARM_FINGERPRINT(STRING_AGG(DISTINCT x.pfx, "," ORDER BY x.pfx))
               FROM UNNEST(h.hops) x), NULL) AS sig_p24
FROM h LEFT JOIN t USING (uuid)
"""

REVTR_SQL = """
WITH r AS (
  SELECT
    raw.src, raw.dst, raw.date AS ts,
    (SELECT h.hop_ip FROM UNNEST(raw.revtr_hops) h WHERE h.hop_type = 1
     ORDER BY h.hop_number LIMIT 1) AS host,
    -- hops outside the client /24 (and not the server itself)
    ARRAY(
      SELECT AS STRUCT h.hop_number AS n, h.hop_ip AS ip, h.asn, h.hop_type AS t
      FROM UNNEST(raw.revtr_hops) h
      WHERE h.hop_ip IS NOT NULL AND h.hop_ip != raw.src
        AND NOT IFNULL(NET.IP_TRUNC(NET.SAFE_IP_FROM_STRING(h.hop_ip), 24)
                       = NET.IP_TRUNC(NET.SAFE_IP_FROM_STRING(raw.dst), 24), FALSE)
      ORDER BY h.hop_number) AS hops
  FROM `measurement-lab.revtr_raw.revtr1` t
  WHERE t.date BETWEEN @start AND @end
    AND raw.label = "ndt_revtr_sidecar"),
c AS (
  SELECT *,
    ARRAY(SELECT asn FROM (
            SELECT x.asn, x.n, LAG(x.asn) OVER (ORDER BY x.n) AS prev
            FROM UNNEST(hops) x WHERE x.asn > 0)
          WHERE prev IS NULL OR prev != asn ORDER BY n) AS as_all,
    ARRAY(SELECT asn FROM (
            SELECT x.asn, x.n, LAG(x.asn) OVER (ORDER BY x.n) AS prev
            FROM UNNEST(hops) x WHERE x.asn > 0 AND x.t IN (5, 6))
          WHERE prev IS NULL OR prev != asn ORDER BY n) AS as_rr
  FROM r)
SELECT
  FARM_FINGERPRINT(CONCAT(src, "|", dst)) AS k,
  FARM_FINGERPRINT(CONCAT(src, "|", host)) AS pk,
  ts, TRUE AS fresh,
  (SELECT FARM_FINGERPRINT(STRING_AGG(x.ip, ">" ORDER BY x.n)) FROM UNNEST(hops) x) AS sig_ip,
  (SELECT FARM_FINGERPRINT(STRING_AGG(CAST(a AS STRING), ">" ORDER BY o))
   FROM UNNEST(as_all) a WITH OFFSET o) AS sig_as,
  (SELECT FARM_FINGERPRINT(STRING_AGG(x.ip, ">" ORDER BY x.n)) FROM UNNEST(hops) x
   WHERE x.t IN (5, 6)) AS sig_rr_ip,
  (SELECT FARM_FINGERPRINT(STRING_AGG(CAST(a AS STRING), ">" ORDER BY o))
   FROM UNNEST(as_rr) a WITH OFFSET o) AS sig_rr_as,
  ARRAY_LENGTH(as_rr) != (SELECT COUNT(DISTINCT a) FROM UNNEST(as_rr) a) AS loop
FROM c
"""


def sig_cols(df):
    return [c for c in df.columns if c.startswith("sig_")]


def fetch(client, sql, start, end):
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "DATE", start),
            bigquery.ScalarQueryParameter("end", "DATE", end),
        ]
    )
    df = client.query(sql, job_config=cfg).to_dataframe()
    n_raw = len(df)
    df = df.dropna(subset=["k", "ts"]).astype({"k": "int64", "ts": "int64"})
    if "fresh" in df:
        df["fresh"] = df["fresh"].fillna(False).astype(bool)
    for c in sig_cols(df) + (["pk"] if "pk" in df else []):
        df[c] = df[c].astype("Int64")
    if "loop" in df:
        df["loop"] = df["loop"].fillna(False).astype(bool)
    return df, n_raw - len(df)


def prepare(df, key="k"):
    """Sort by (key, ts); return sorted df, group ids, timestamps, group start/end indices."""
    df = df.sort_values([key, "ts"], kind="stable")
    g = pd.factorize(df[key].to_numpy(), sort=False)[0]
    # factorize on already-sorted keys yields non-decreasing group ids
    ts = df["ts"].to_numpy(np.int64)
    starts = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
    ends = np.r_[starts[1:], len(g)]
    return df, g, ts, starts, ends


def fill_misses(g, ts, starts, ends, ttl):
    """Miss mask for a cache that measures on a miss and serves hits until
    the measurement is `ttl` seconds old. Vectorised across pairs: each
    iteration advances every active pair to its next miss via searchsorted."""
    t0 = ts.min()
    key = g.astype(np.int64) * (1 << 34) + (ts - t0)
    cur = starts.copy()
    end = ends
    miss = np.zeros(len(ts), bool)
    miss[starts] = True
    active = np.ones(len(starts), bool)
    while active.any():
        idx = np.flatnonzero(active)
        target = g[cur[idx]].astype(np.int64) * (1 << 34) + (ts[cur[idx]] - t0) + ttl
        nxt = np.searchsorted(key, target, side="left")
        ok = nxt < end[idx]
        miss[nxt[ok]] = True
        cur[idx[ok]] = nxt[ok]
        active[idx[~ok]] = False
    return miss


def analyse(name, df):
    _, g, ts, starts, ends = prepare(df)
    n, pairs = len(ts), len(starts)
    same = np.r_[False, g[1:] == g[:-1]]
    gaps = np.diff(ts, prepend=ts[0])[same]

    sizes = ends - starts
    order = np.argsort(sizes)[::-1]
    top1 = sizes[order[0]]
    rows = []
    for T in THRESHOLDS_S:
        sliding_hits = int((gaps <= T).sum())
        fill_hits = n - int(fill_misses(g, ts, starts, ends, T).sum())
        rows.append(
            {
                "stream": name,
                "T_s": T,
                "sliding_hit_rate": sliding_hits / n,
                "fill_hit_rate": fill_hits / n,
                "measurements_needed": n - fill_hits,
            }
        )
    summary = {
        "stream": name,
        "events": n,
        "pairs": pairs,
        "pairs_with_repeat": int((sizes > 1).sum()),
        "events_in_repeat_pairs": int(sizes[sizes > 1].sum()),
        "largest_pair_events": int(top1),
        "largest_pair_share": top1 / n,
        "top10_pairs_share": sizes[order[:10]].sum() / n,
        "ceiling_hit_rate_T_inf": (n - pairs) / n,
        "gap_quantiles_s": {q: float(np.quantile(gaps, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
        if len(gaps)
        else {},
    }
    if "fresh" in df:
        summary["fresh_share"] = float(df["fresh"].mean())
    return pd.DataFrame(rows), summary, gaps


def analyse_without_top(name, df, top_n):
    sizes = df["k"].value_counts()
    drop = set(sizes.index[:top_n])
    return analyse(f"{name} (excl. top {top_n} pairs)", df[~df["k"].isin(drop)])


def analyse_paths(name, df):
    """Path change between consecutive fresh measurements of the same
    (server, host), and the fill policy re-run per host on that subsequence
    with hits split by whether the served path matches the event's own path.
    One pass per signature level."""
    by_gap, by_T, summary = [], [], {}
    for level in sig_cols(df):
        sub = df[df["fresh"] & df[level].notna() & df["pk"].notna()]
        sub, g, ts, starts, ends = prepare(sub, key="pk")
        sig = sub[level].to_numpy(np.int64)
        n = len(ts)
        same = np.r_[False, g[1:] == g[:-1]]
        gap = np.diff(ts, prepend=ts[0])[same]
        changed = (sig != np.r_[sig[0], sig[:-1]])[same]
        if "loop" in sub:
            lp = sub["loop"].to_numpy()
            loop = (lp | np.r_[False, lp[:-1]])[same]
        else:
            loop = np.zeros(len(gap), bool)
        b = pd.cut(gap, GAP_EDGES_S, right=False)
        t = (
            pd.DataFrame({"bin": b, "changed": changed, "changed_loop": changed & loop})
            .groupby("bin", observed=False)
            .agg(pairs=("changed", "size"), changed=("changed", "sum"), changed_loop=("changed_loop", "sum"))
            .reset_index()
        )
        t.insert(0, "level", level)
        t.insert(0, "stream", name)
        t["gap_lo_s"] = [iv.left for iv in t["bin"]]
        t["gap_hi_s"] = [iv.right for iv in t["bin"]]
        t["same"] = t["pairs"] - t["changed"]
        t["p_changed"] = t["changed"] / t["pairs"].where(t["pairs"] > 0)
        by_gap.append(t.drop(columns="bin"))

        idx = np.arange(n)
        for T in THRESHOLDS_S:
            miss = fill_misses(g, ts, starts, ends, T)
            # every pair's first event is a miss, so the running max stays in-pair
            serve = np.maximum.accumulate(np.where(miss, idx, 0))
            hit = ~miss
            stale = hit & (sig != sig[serve])
            by_T.append(
                {
                    "stream": name,
                    "level": level,
                    "T_s": T,
                    "events": n,
                    "fill_hit_rate": hit.sum() / n,
                    "same_path_hit_rate": (hit & ~stale).sum() / n,
                    "changed_path_hit_rate": stale.sum() / n,
                    "changed_share_of_hits": stale.sum() / max(hit.sum(), 1),
                }
            )
        summary[level] = {
            "events": n,
            "hosts": len(starts),
            "consecutive_pairs": int(same.sum()),
            "p_changed_overall": float(changed.mean()) if len(changed) else None,
            "share_of_changes_with_as_loop": float((changed & loop).sum() / max(changed.sum(), 1)),
        }
    return pd.concat(by_gap, ignore_index=True), pd.DataFrame(by_T), summary


def plot(results, gaps_by_stream, by_gap, by_T, stamp, out):
    fig, ax = plt.subplots(2, 2, figsize=(12, 9))
    a = ax[0, 0]
    for name, gaps in gaps_by_stream.items():
        g = np.sort(np.maximum(gaps, 1))
        a.plot(g, np.arange(1, len(g) + 1) / len(g), lw=2, color=STREAM_COLORS[name], label=name)
    a.set_xscale("log")
    a.set_xlabel("inter-arrival within pair (s)")
    a.set_ylabel("CDF over repeat events")
    a.set_title("inter-arrival, all events", fontsize=10)
    for x, lbl in [(60, "1m"), (600, "10m"), (3600, "1h"), (86400, "1d")]:
        a.axvline(x, color="grey", lw=0.5, ls=":")
        a.text(x, 0.02, lbl, fontsize=8, color="grey")
    a.legend(fontsize=8, loc="upper left")

    a = ax[0, 1]
    for name, df in results[results["stream"].isin(STREAM_COLORS)].groupby("stream", sort=False):
        c = STREAM_COLORS[name]
        a.plot(df["T_s"], df["fill_hit_rate"], lw=2, marker="o", color=c, label=f"{name} fill")
        a.plot(df["T_s"], df["sliding_hit_rate"], lw=2, ls="--", color=c, alpha=0.6)
    a.set_xscale("log")
    a.set_xlabel("staleness threshold T (s)")
    a.set_ylabel("share of measurements avoided")
    a.set_ylim(0, 1)
    a.set_title("reuse, all events", fontsize=10)
    a.legend(fontsize=8, loc="lower right", title="solid = fill TTL, dashed = sliding", title_fontsize=8)

    a = ax[1, 0]
    for (name, level), df in by_gap.groupby(["stream", "level"], sort=False):
        df = df[df["pairs"] >= 100]
        mid = np.sqrt(np.maximum(df["gap_lo_s"], 0.5) * np.where(np.isinf(df["gap_hi_s"]), 172800, df["gap_hi_s"]))
        ls, mk, al = LEVEL_STYLES[level]
        a.plot(mid, df["p_changed"], lw=2, marker=mk, ls=ls, alpha=al,
               color=STREAM_COLORS[name], label=f"{name} {level[4:]}")
    a.set_xscale("log")
    a.set_xlabel("gap between consecutive fresh measurements (s, bin midpoint)")
    a.set_ylabel("P(path changed)")
    a.set_ylim(0, 1)
    a.set_title("path change vs gap, same host (bins with >= 100 pairs)", fontsize=10)
    a.legend(fontsize=8, loc="upper left")

    a = ax[1, 1]
    for (name, level), df in by_T.groupby(["stream", "level"], sort=False):
        ls, mk, al = LEVEL_STYLES[level]
        a.plot(df["T_s"], df["changed_share_of_hits"], lw=2, marker=mk, ls=ls, alpha=al,
               color=STREAM_COLORS[name], label=f"{name} {level[4:]}")
    a.set_xscale("log")
    a.set_xlabel("staleness threshold T (s)")
    a.set_ylabel("share of cache hits served a changed path")
    a.set_ylim(0, 1)
    a.set_title("fill policy per host, fresh measurements", fontsize=10)
    a.legend(fontsize=8, loc="upper left")

    fig.suptitle(stamp, fontsize=8, x=0.99, ha="right")
    fig.tight_layout()
    fig.savefig(out, dpi=150)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-20")
    ap.add_argument("--end", default="2026-09-22")
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    client = bigquery.Client(project=BILLING_PROJECT)
    frames, dropped = {}, {}
    for name, sql in [("ndt", NDT_SQL), ("scamper", SCAMPER_SQL), ("revtr", REVTR_SQL)]:
        frames[name], dropped[name] = fetch(client, sql, args.start, args.end)
        print(f"{name}: {len(frames[name]):,} rows ({dropped[name]:,} dropped: no key/ts)", flush=True)

    results, summaries, gaps = [], [], {}
    path_gap, path_T, path_summary = [], [], {}
    for name, df in frames.items():
        for fn in (lambda: analyse(name, df), lambda: analyse_without_top(name, df, 10)):
            r, s, gp = fn()
            s["rows_dropped_no_key_or_ts"] = dropped[name]
            results.append(r)
            summaries.append(s)
            if s["stream"] == name:
                gaps[name] = gp
        if sig_cols(df):
            bg, bt, ps = analyse_paths(name, df)
            path_gap.append(bg)
            path_T.append(bt)
            path_summary[name] = ps
    results = pd.concat(results, ignore_index=True)
    path_gap = pd.concat(path_gap, ignore_index=True)
    path_T = pd.concat(path_T, ignore_index=True)
    results.to_csv(out / "reuse_by_threshold.csv", index=False)
    path_gap.to_csv(out / "path_change_by_gap.csv", index=False)
    path_T.to_csv(out / "stale_by_threshold.csv", index=False)
    meta = {
        "window": [args.start, args.end],
        "provenance": "measured",
        "generated_by": Path(__file__).name,
        "generated_on": pd.Timestamp.now("UTC").strftime("%Y-%m-%d"),
        "summaries": summaries,
        "path_change": path_summary,
    }
    (out / "summary.json").write_text(json.dumps(meta, indent=2, default=float))
    stamp = f"measured -- {Path(__file__).name} -- {args.start}..{args.end} -- generated {meta['generated_on']}"
    plot(results, gaps, path_gap, path_T, stamp, out / "reuse_interarrival.png")
    print(json.dumps(meta, indent=2, default=float))
    print(results.to_string(index=False))
    print(path_gap.to_string(index=False))
    print(path_T.to_string(index=False))


if __name__ == "__main__":
    main()
