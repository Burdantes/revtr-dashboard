"""Daily rebuild of the per-destination-AS-per-day table (as_distribution_7d).

Runs sql/05_as_distribution.sql (shipped to the VM as /work/05_as_distribution.sql).
Billed to nsf-2148275-66720: the query CREATEs a table there, and a job billed to
measurement-lab cannot create tables cross-project (it still reads measurement-lab
revtr_raw / ndt_raw cross-project). The dashboard's /api/as_distribution reads the
resulting table — the longest-prefix match over ~1.1M RouteViews prefixes takes
~3 min, too slow for a live request.
"""
from google.cloud import bigquery

client = bigquery.Client(project="nsf-2148275-66720")
sql = open("/work/05_as_distribution.sql").read()
job = client.query(sql)
job.result()
gib = (job.total_bytes_billed or 0) / 2**30
print("as_distribution_7d rebuilt  billed=%.2f GiB  job=%s" % (gib, job.job_id))
