#!/usr/bin/env bash
set -euo pipefail

project_id="${1:-${GCP_PROJECT_ID:-}}"
github_owner="${2:-PMK1991}"
github_repo="${3:-FirmAware}"
region="${GCP_REGION:-us-central1}"
pool_id="${WIF_POOL_ID:-github-actions}"
state_bucket="${TF_STATE_BUCKET:-${project_id}-firmaware-tf-state}"

if [[ -z "${project_id}" ]]; then
  echo "Usage: $0 PROJECT_ID [GITHUB_OWNER] [GITHUB_REPO]" >&2
  exit 2
fi

gcloud services enable \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  storage.googleapis.com \
  sts.googleapis.com \
  --project "${project_id}"

if ! gcloud storage buckets describe "gs://${state_bucket}" \
  --project "${project_id}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${state_bucket}" \
    --project "${project_id}" \
    --location "${region}" \
    --uniform-bucket-level-access
fi
gcloud storage buckets update "gs://${state_bucket}" --versioning

if ! gcloud iam workload-identity-pools describe "${pool_id}" \
  --project "${project_id}" \
  --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${pool_id}" \
    --project "${project_id}" \
    --location global \
    --display-name "GitHub Actions"
fi

project_number="$(gcloud projects describe "${project_id}" \
  --format='value(projectNumber)')"
echo "state_bucket=${state_bucket}"
echo "workload_identity_pool=projects/${project_number}/locations/global/workloadIdentityPools/${pool_id}"
echo "provider will be created by Terraform for ${github_owner}/${github_repo}"
echo "terraform init -backend-config=bucket=${state_bucket} -backend-config=prefix=firmaware/ENV"
