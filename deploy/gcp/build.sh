#!/usr/bin/env bash
set -euo pipefail

env_name="${1:-dev}"
project_id="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
region="${GCP_REGION:-us-central1}"
repository="${FIRMAWARE_ARTIFACT_REPOSITORY:-firmaware}"
git_sha="${FIRMAWARE_GIT_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"

if [[ -z "${project_id}" || "${project_id}" == "(unset)" ]]; then
  echo "GCP_PROJECT_ID or an active gcloud project is required" >&2
  exit 2
fi
if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

image="${region}-docker.pkg.dev/${project_id}/${repository}/firmaware"
tagged_image="${image}:${git_sha}"

gcloud auth configure-docker "${region}-docker.pkg.dev" --quiet
if digest="$(gcloud artifacts docker images describe \
  "${tagged_image}" \
  --project "${project_id}" \
  --format='value(image_summary.digest)' 2>/dev/null)" && \
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  image_digest="${image}@${digest}"
  echo "image_digest=${image_digest}"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image_digest=${image_digest}" >> "${GITHUB_OUTPUT}"
    echo "digest=${digest}" >> "${GITHUB_OUTPUT}"
  fi
  exit 0
fi

# --target runtime explicitly. Without it Docker builds the last stage in the
# file, which is now `azureml` -- an image whose entrypoint is the AML inference
# server, not the firmaware CLI that Cloud Run Jobs invoke.
docker build --platform linux/amd64 --target runtime --tag "${tagged_image}" .
docker push "${tagged_image}"

digest="$(gcloud artifacts docker images describe \
  "${tagged_image}" \
  --project "${project_id}" \
  --format='value(image_summary.digest)')"
if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Artifact Registry returned an invalid digest: ${digest}" >&2
  exit 1
fi

image_digest="${image}@${digest}"
echo "image_digest=${image_digest}"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "image_digest=${image_digest}" >> "${GITHUB_OUTPUT}"
  echo "digest=${digest}" >> "${GITHUB_OUTPUT}"
fi
