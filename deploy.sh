#!/usr/bin/env bash
#
# Deploy the revTr Health Monitor API to Google Cloud Run.
#
# Usage:
#   cd code/analysis/revtr_monitoring
#   bash deploy.sh <GCP_PROJECT_ID>
#
# Prerequisites:
#   - gcloud CLI authenticated
#   - A service account with BigQuery access
#
# After deployment, update the API_BASE in your GitHub Pages index.html
# to the Cloud Run URL printed at the end.
#
set -euo pipefail

PROJECT="${1:?Usage: deploy.sh <GCP_PROJECT_ID>}"
SERVICE="revtr-health-api"
REGION="us-central1"

echo "==> Building and deploying to Cloud Run in project: ${PROJECT}"
echo "    Service: ${SERVICE}"
echo "    Region:  ${REGION}"
echo

# Enable required APIs
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  --project="${PROJECT}" --quiet

# Deploy directly from source (Cloud Build will build the Dockerfile)
gcloud run deploy "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --source=. \
  --allow-unauthenticated \
  --memory=512Mi \
  --timeout=120 \
  --max-instances=3 \
  --set-env-vars="REVTR_API_KEY=${REVTR_API_KEY:?Set REVTR_API_KEY in your environment before deploying}" \
  --quiet

# Get the URL
URL=$(gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --format="value(status.url)")

echo
echo "==> Deployed successfully!"
echo "    API URL: ${URL}"
echo
echo "    Update your GitHub Pages index.html:"
echo "      window.REVTR_API_BASE = '${URL}';"
echo
