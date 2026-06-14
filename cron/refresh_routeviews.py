"""Weekly refresh of the RouteViews prefix->AS table (routeviews_pfx2as).

Downloads the latest CAIDA routeviews-rv2 pfx2as snapshot, parses it to
(network, mask, asn), and replaces nsf-2148275-66720.revtr_dashboard.routeviews_pfx2as.
Keeps the IP->AS mapping current so the 'unmapped' bucket stays small. Load jobs
are free; run in nsf (the destination project).
"""
import datetime
import gzip
import io
import re
import urllib.request

from google.cloud import bigquery

BASE = "https://publicdata.caida.org/datasets/routing/routeviews-prefix2as"
TABLE = "nsf-2148275-66720.revtr_dashboard.routeviews_pfx2as"
FILE_RE = re.compile(r"routeviews-rv2-\d{8}-\d{4}\.pfx2as\.gz")


def latest_url():
    today = datetime.date.today()
    for back in range(4):  # walk back up to ~4 months if the current dir is empty
        m = today.replace(day=1) - datetime.timedelta(days=back * 28)
        ym = "%04d/%02d" % (m.year, m.month)
        try:
            html = urllib.request.urlopen("%s/%s/" % (BASE, ym), timeout=60).read().decode("utf-8", "replace")
        except Exception:
            continue
        files = sorted(set(FILE_RE.findall(html)))
        if files:
            return "%s/%s/%s" % (BASE, ym, files[-1])
    raise SystemExit("no routeviews pfx2as file found")


url = latest_url()
print("downloading", url, flush=True)
raw = urllib.request.urlopen(url, timeout=300).read()

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
        buf.write("%s\t%d\t%s\n" % (net, mask, m.group(1)))
        n += 1
buf.seek(0)
print("parsed", n, "prefixes", flush=True)

client = bigquery.Client(project="nsf-2148275-66720")
cfg = bigquery.LoadJobConfig(
    schema=[bigquery.SchemaField("network", "STRING"),
            bigquery.SchemaField("mask", "INTEGER"),
            bigquery.SchemaField("asn", "INTEGER")],
    write_disposition="WRITE_TRUNCATE",
    source_format=bigquery.SourceFormat.CSV,
    field_delimiter="\t",
)
data = io.BytesIO(buf.getvalue().encode("utf-8"))
load = client.load_table_from_file(data, TABLE, job_config=cfg)
load.result()
print("loaded", load.output_rows, "rows into", TABLE, flush=True)
