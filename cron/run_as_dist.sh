#!/usr/bin/env bash
# Daily: rebuild as_distribution_7d (per-AS-per-day) in a one-off container.
set -euo pipefail
DIR=/home/loqmansalamatian/revtr-rollup
echo "=== $(date -u +%FT%TZ) as_dist start ==="
docker run --rm \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json \
  -v /home/loqmansalamatian/.config/gcloud/application_default_credentials.json:/creds/adc.json:ro \
  -v "${DIR}:/work:ro" \
  --entrypoint python revtr-monitor:latest /work/as_dist_runner.py
echo "=== $(date -u +%FT%TZ) as_dist end ==="
