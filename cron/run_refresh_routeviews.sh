#!/usr/bin/env bash
# Weekly: refresh the RouteViews prefix->AS table from CAIDA.
set -euo pipefail
DIR=$HOME/revtr-rollup
echo "=== $(date -u +%FT%TZ) routeviews refresh start ==="
docker run --rm \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json \
  -v $HOME/.config/gcloud/application_default_credentials.json:/creds/adc.json:ro \
  -v "${DIR}:/work:ro" \
  --entrypoint python revtr-monitor:latest /work/refresh_routeviews.py
echo "=== $(date -u +%FT%TZ) routeviews refresh end ==="
