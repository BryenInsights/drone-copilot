#!/usr/bin/env bash
# One-time GCP project setup for drone-copilot.
# Enables required APIs, creates service account, and configures IAM.
#
# Usage: ./setup-gcp.sh <PROJECT_ID>

set -euo pipefail

PROJECT_ID="${1:?Usage: setup-gcp.sh <PROJECT_ID>}"
REGION="${REGION:-us-central1}"
SA_NAME="drone-copilot-deployer"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Setting active project to ${PROJECT_ID}..."
gcloud config set project "${PROJECT_ID}"

echo "==> Enabling required APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    containerregistry.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    --project "${PROJECT_ID}"

echo "==> Creating service account: ${SA_NAME}..."
if gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}" &>/dev/null; then
    echo "    Service account already exists."
else
    gcloud iam service-accounts create "${SA_NAME}" \
        --display-name="Drone Copilot Deployer" \
        --project "${PROJECT_ID}"
fi

echo "==> Granting IAM roles..."
for ROLE in \
    roles/run.admin \
    roles/cloudbuild.builds.editor \
    roles/storage.admin \
    roles/iam.serviceAccountUser \
    roles/secretmanager.secretAccessor; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="${ROLE}" \
        --quiet
done

echo "==> Setting default Cloud Run region..."
gcloud config set run/region "${REGION}"

echo ""
echo "==> GCP project setup complete!"
echo "Project:         ${PROJECT_ID}"
echo "Region:          ${REGION}"
echo "Service Account: ${SA_EMAIL}"
echo ""
echo "Next steps:"
echo "  1. Store your Gemini API key:  gcloud secrets create gemini-api-key --data-file=- <<< 'YOUR_KEY'"
echo "  2. Deploy the backend:         ./deploy.sh ${PROJECT_ID} YOUR_GEMINI_API_KEY"
