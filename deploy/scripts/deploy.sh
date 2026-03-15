#!/usr/bin/env bash
# Automated deployment script for drone-copilot backend to Cloud Run.
# Uses Vertex AI (no API key needed — Cloud Run authenticates via GCP).
#
# Usage: ./deploy.sh <PROJECT_ID>

set -euo pipefail

PROJECT_ID="${1:?Usage: deploy.sh <PROJECT_ID>}"

SERVICE_NAME="drone-copilot-backend"
REGION="${REGION:-us-central1}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "==> Building Docker image via Cloud Build..."
cd "$(git rev-parse --show-toplevel)/backend"
gcloud builds submit --tag "${IMAGE}" --project "${PROJECT_ID}"

echo "==> Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --image "${IMAGE}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --timeout=3600 \
    --concurrency=50 \
    --session-affinity \
    --min-instances=1 \
    --max-instances=3 \
    --cpu=1 \
    --memory=512Mi \
    --port=8080 \
    --set-env-vars="USE_VERTEX_AI=true,GCP_PROJECT=${PROJECT_ID},GCP_LOCATION=${REGION}" \
    --allow-unauthenticated

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format='value(status.url)')

echo ""
echo "==> Deployment complete!"
echo "Service URL: ${SERVICE_URL}"
echo "WebSocket:   ${SERVICE_URL/https/wss}/ws"
echo "Health:      ${SERVICE_URL}/health"
