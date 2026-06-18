"""Daily revtr daily_summary rollup, run on the dashboard VM via cron.

Runs the idempotent 3-day rollup MERGE (sql/04_scheduled_rollup.sql, shipped
to the VM as /work/rollup.sql). Jobs are billed to measurement-lab (the deploy
ADC account has jobs.create there + read on measurement-lab +
write on nsf-2148275-66720), so this fills the existing daily_summary table
without a BigQuery scheduled-query / Data Transfer config.
"""
from google.cloud import bigquery

BILLING_PROJECT = "measurement-lab"

client = bigquery.Client(project=BILLING_PROJECT)
with open("/work/rollup.sql") as fh:
    sql = fh.read()
job = client.query(sql)
job.result()
gib = (job.total_bytes_billed or 0) / 2**30
print(f"rollup OK  job_id={job.job_id}  billed={gib:.2f} GiB  project={BILLING_PROJECT}")
