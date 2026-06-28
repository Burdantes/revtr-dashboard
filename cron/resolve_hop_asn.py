"""Resolve historical revtr hop IPs -> ASN with a Pytricia radix trie.

For pre-~May-2025 revtr data the per-hop `asn` field is empty, so type-2
"symmetry assumption" hops can't be split into intradomain (11) / interdomain
(12). We reconstruct each hop's AS by longest-prefix-matching its `hop_ip`
against the RouteViews snapshot for that month. Doing the LPM as a SQL join over
all hop IPs blows BigQuery's on-demand CPU/bytes guardrail; a Pytricia trie does
the same ~1.5M lookups/month in seconds in memory.

Per month it: builds a trie from routeviews_pfx2as_hist[ym], pulls that month's
distinct IPv4 hop IPs, looks each up, and writes matches to:

    nsf-2148275-66720.revtr_dashboard.hist_ip_asn  (ym, hop_ip, asn)

Idempotent per month. Unmatched / IPv6 IPs are simply omitted (the rollup treats
a missing hop_ip -> asn as "no AS"). Run on the VM via the revtr-resolver image.

Usage:
    python resolve_hop_asn.py 2024-06            # single month
    python resolve_hop_asn.py 2023-09 2025-05    # inclusive month range
"""
import datetime
import io
import sys

import pytricia
from google.cloud import bigquery

READ_PROJECT = "measurement-lab"           # billing for the revtr scan
NSF = "nsf-2148275-66720"
HIST_PFX = f"{NSF}.revtr_dashboard.routeviews_pfx2as_hist"
CACHE = f"{NSF}.revtr_dashboard.hist_ip_asn"
SCHEMA = [
    bigquery.SchemaField("ym", "STRING"),
    bigquery.SchemaField("hop_ip", "STRING"),
    bigquery.SchemaField("asn", "INTEGER"),
]


def month_bounds(ym: str) -> tuple[str, str]:
    y, m = map(int, ym.split("-"))
    nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = datetime.date(*nm, 1) - datetime.timedelta(days=1)
    return f"{ym}-01", end.isoformat()


def build_trie(client: bigquery.Client, ym: str) -> "pytricia.PyTricia":
    pyt = pytricia.PyTricia()
    n = 0
    for r in client.query(f"SELECT network, mask, asn FROM `{HIST_PFX}` WHERE ym = '{ym}'").result():
        pyt[f"{r['network']}/{r['mask']}"] = int(r["asn"])
        n += 1
    print(f"  trie: {n} prefixes for {ym}", flush=True)
    return pyt


def resolve_month(rc: bigquery.Client, wc: bigquery.Client, ym: str) -> None:
    print(f"== {ym} ==", flush=True)
    pyt = build_trie(rc, ym)
    start, end = month_bounds(ym)
    q = (f"SELECT DISTINCT h.hop_ip AS ip "
         f"FROM `{READ_PROJECT}.revtr_raw.revtr1` r, UNNEST(r.raw.revtr_hops) h "
         f"WHERE r.date BETWEEN DATE('{start}') AND DATE('{end}') AND h.hop_ip IS NOT NULL")
    buf = io.StringIO()
    seen = matched = 0
    for row in rc.query(q).result():
        ip = row["ip"]
        seen += 1
        if ":" in ip:           # rv2 pfx2as is IPv4-only
            continue
        asn = pyt.get(ip)        # longest-prefix match, or None
        if asn is None:
            continue
        buf.write(f"{ym}\t{ip}\t{asn}\n")
        matched += 1
    print(f"  {seen} distinct hop IPs, {matched} matched ({100*matched/max(seen,1):.1f}%)", flush=True)

    try:
        wc.get_table(CACHE)
    except Exception:
        wc.create_table(bigquery.Table(CACHE, schema=SCHEMA))
        print(f"  created {CACHE}", flush=True)
    wc.query(f"DELETE FROM `{CACHE}` WHERE ym = '{ym}'").result()
    cfg = bigquery.LoadJobConfig(
        schema=SCHEMA, write_disposition="WRITE_APPEND",
        source_format=bigquery.SourceFormat.CSV, field_delimiter="\t",
    )
    job = wc.load_table_from_file(io.BytesIO(buf.getvalue().encode()), CACHE, job_config=cfg)
    job.result()
    print(f"  loaded {job.output_rows} rows into hist_ip_asn for {ym}", flush=True)


def months(lo: str, hi: str):
    y, m = map(int, lo.split("-"))
    ey, em = map(int, hi.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m = 1 if m == 12 else m + 1
        y = y + 1 if m == 1 else y


def main(argv: list[str]) -> None:
    lo = argv[1]
    hi = argv[2] if len(argv) > 2 else lo
    rc = bigquery.Client(project=READ_PROJECT)
    wc = bigquery.Client(project=NSF)
    for ym in months(lo, hi):
        resolve_month(rc, wc, ym)


if __name__ == "__main__":
    main(sys.argv)
