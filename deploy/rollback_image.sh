#!/usr/bin/env bash
set -euo pipefail

env_name="${1:-}"
image_digest="${2:-}"
if [[ ! "${env_name}" =~ ^(dev|prod)$ ]] || \
  [[ ! "${image_digest}" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "Usage: $0 {dev|prod} REGION-docker.pkg.dev/PROJECT/REPO/IMAGE@sha256:DIGEST" >&2
  exit 2
fi

project_variable="$(printf '%s_GCP_PROJECT_ID' "${env_name}" | tr '[:lower:]' '[:upper:]')"
project_id="${!project_variable:-${GCP_PROJECT_ID:-}}"
state_variable="$(printf '%s_TF_STATE_BUCKET' "${env_name}" | tr '[:lower:]' '[:upper:]')"
state_bucket="${!state_variable:-${TF_STATE_BUCKET:-}}"
if [[ -z "${project_id}" || -z "${state_bucket}" ]]; then
  echo "${project_variable} and ${state_variable} are required" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
terraform -chdir="${root}/infra" init \
  -backend-config="bucket=${state_bucket}" \
  -backend-config="prefix=firmaware/${env_name}"
terraform -chdir="${root}/infra" apply \
  -auto-approve \
  -var-file="envs/${env_name}.tfvars" \
  -var="project_id=${project_id}" \
  -var="state_bucket_name=${state_bucket}" \
  -var="image_digest=${image_digest}"
