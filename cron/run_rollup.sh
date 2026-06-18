#!/usr/bin/env bash
# Daily cron wrapper: runs the revtr daily_summary rollup in a one-off container
# (reuses the revtr-monitor image + the deploy ADC). Billed to measurement-lab.
set -euo pipefail
DIR=$HOME/revtr-rollup
echo "=== $(date -u +%FT%TZ) rollup start ==="
docker run --rm \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json \
  -v $HOME/.config/gcloud/application_default_credentials.json:/creds/adc.json:ro \
  -v "${DIR}:/work:ro" \
  --entrypoint python revtr-monitor:latest /work/rollup_runner.py
echo "=== $(date -u +%FT%TZ) rollup end ==="
