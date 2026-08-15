#!/usr/bin/env bash
# Hourly: evaluate revTr health and email on high/critical anomalies.
#
# Same one-off-container pattern as run_rollup.sh (reuses the revtr-monitor
# image + the deploy ADC). BigQuery is billed to measurement-lab; ~1.33 GiB
# scanned per run.
#
# Two mounts that differ from the other cron wrappers:
#   - /state is READ-WRITE. It holds the dedup state file, which must survive
#     across runs or every hourly run re-sends the same alert.
#   - alert.env carries the SMTP app password. It is chmod 600 and lives
#     outside the repo, so the credential is never committed.
set -euo pipefail
DIR=$HOME/revtr-rollup
STATE_DIR=$HOME/revtr-alerts

mkdir -p "${STATE_DIR}"

echo "=== $(date -u +%FT%TZ) alert check start ==="
docker run --rm \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json \
  -e ALERT_STATE_PATH=/state/state.json \
  --env-file "${STATE_DIR}/alert.env" \
  -v $HOME/.config/gcloud/application_default_credentials.json:/creds/adc.json:ro \
  -v "${DIR}:/work:ro" \
  -v "${STATE_DIR}:/state" \
  --entrypoint python revtr-monitor:latest /work/alert_runner.py "$@"
echo "=== $(date -u +%FT%TZ) alert check end ==="
