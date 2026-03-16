#!/usr/bin/env bash
# Automated deployment script for drone-copilot backend to Cloud Run.
#
# Prerequisites: Create a .env file in the project root with GEMINI_API_KEY.
# See .env.example for the template.
#
# Usage: ./deploy.sh <PROJECT_ID>

set -euo pipefail

PROJECT_ID="${1:?Usage: deploy.sh <PROJECT_ID>}"

# Load GEMINI_API_KEY from root .env file
REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ -f "${REPO_ROOT}/.env" ]; then
    GEMINI_API_KEY=$(grep -E '^GEMINI_API_KEY=' "${REPO_ROOT}/.env" | cut -d'=' -f2- | tr -d '"')
fi

if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "ERROR: GEMINI_API_KEY not found."
    echo "Create a .env file in the project root with: GEMINI_API_KEY=your-key"
    echo "See .env.example for the template."
    exit 1
fi

SERVICE_NAME="drone-copilot-backend"
REGION="${REGION:-us-central1}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "==> Building Docker image via Cloud Build..."
cd "${REPO_ROOT}/backend"
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
    --set-env-vars="USE_VERTEX_AI=false,GEMINI_API_KEY=${GEMINI_API_KEY}" \
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
