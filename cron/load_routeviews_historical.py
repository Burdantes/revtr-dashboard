"""Load ONE RouteViews pfx2as snapshot per month into a historical prefix table.

For the pre-~May-2025 revtr data the per-hop `asn` field is empty (the pipeline
had not yet run IP->AS annotation), so the type-2 "symmetry assumption" hops
cannot be split into intradomain (11) / interdomain (12) from the raw data. We
reconstruct each hop's AS by longest-prefix-matching its `hop_ip` against the
RouteViews prefix table that was current THAT month. This script downloads one
snapshot per requested month and appends it to:

    nsf-2148275-66720.revtr_dashboard.routeviews_pfx2as_hist  (ym, network, mask, asn)

Idempotent per month (deletes the month's rows before re-appending). Mirrors the
parse logic of refresh_routeviews.py. Run on the VM (CAIDA fetch + nsf write).

Usage:
    python load_routeviews_historical.py 2023-09 2025-05   # inclusive month range
    python load_routeviews_historical.py 2024-06           # single month
"""
import datetime
import gzip
import io
import re
import sys
import urllib.request

from google.cloud import bigquery

BASE = "https://publicdata.caida.org/datasets/routing/routeviews-prefix2as"
TABLE = "nsf-2148275-66720.revtr_dashboard.routeviews_pfx2as_hist"
FILE_RE = re.compile(r"routeviews-rv2-\d{8}-\d{4}\.pfx2as\.gz")

SCHEMA = [
    bigquery.SchemaField("ym", "STRING"),
    bigquery.SchemaField("network", "STRING"),
    bigquery.SchemaField("mask", "INTEGER"),
    bigquery.SchemaField("asn", "INTEGER"),
]


def file_for_month(year: int, month: int) -> str:
    """Return the URL of the first pfx2as snapshot in YYYY/MM (one per month)."""
    ym = "%04d/%02d" % (year, month)
    html = urllib.request.urlopen("%s/%s/" % (BASE, ym), timeout=120).read().decode("utf-8", "replace")
    files = sorted(set(FILE_RE.findall(html)))
    if not files:
        raise SystemExit("no routeviews pfx2as file found for %s" % ym)
    return "%s/%s/%s" % (BASE, ym, files[0])  # first snapshot of the month


def parse(raw: bytes, ym: str) -> tuple[io.BytesIO, int]:
    buf = io.StringIO()
    n = 0
    with gzip.open(io.BytesIO(raw), "rt") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 3:
                continue
            net, length, asf = p[0], p[1], p[2]
            m = re.match(r"(\d+)", re.split(r"[,_]", asf)[0])  # first AS of a MOAS/AS-set
            if not m:
                continue
            try:
                mask = int(length)
            except ValueError:
                continue
            if not 1 <= mask <= 32:
                continue
            buf.write("%s\t%s\t%d\t%s\n" % (ym, net, mask, m.group(1)))
            n += 1
    return io.BytesIO(buf.getvalue().encode("utf-8")), n


def ensure_table(client: bigquery.Client) -> None:
    try:
        client.get_table(TABLE)
    except Exception:
        client.create_table(bigquery.Table(TABLE, schema=SCHEMA))
        print("created", TABLE, flush=True)


def load_month(client: bigquery.Client, year: int, month: int) -> None:
    ym = "%04d-%02d" % (year, month)
    url = file_for_month(year, month)
    print("downloading", url, flush=True)
    raw = urllib.request.urlopen(url, timeout=600).read()
    data, n = parse(raw, ym)
    print("parsed", n, "prefixes for", ym, flush=True)
    # Idempotent: drop any prior rows for this month before appending.
    client.query(f"DELETE FROM `{TABLE}` WHERE ym = '{ym}'").result()
    cfg = bigquery.LoadJobConfig(
        schema=SCHEMA,
        write_disposition="WRITE_APPEND",
        source_format=bigquery.SourceFormat.CSV,
        field_delimiter="\t",
    )
    job = client.load_table_from_file(data, TABLE, job_config=cfg)
    job.result()
    print("loaded", job.output_rows, "rows for", ym, flush=True)


def months(lo: str, hi: str):
    y, m = map(int, lo.split("-"))
    ey, em = map(int, hi.split("-"))
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def main(argv: list[str]) -> None:
    lo = argv[1]
    hi = argv[2] if len(argv) > 2 else lo
    client = bigquery.Client(project="nsf-2148275-66720")
    ensure_table(client)
    for y, m in months(lo, hi):
        load_month(client, y, m)


if __name__ == "__main__":
    main(sys.argv)
