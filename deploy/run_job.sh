#!/usr/bin/env bash
set -euo pipefail

job="${1:-}"
env_name="${2:-}"
shift $(( $# >= 2 ? 2 : $# ))

if [[ ! "${job}" =~ ^(validate|train|predict)$ ]]; then
  echo "Usage: $0 {validate|train|predict} {dev|prod} [gcloud execute flags]" >&2
  exit 2
fi
if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Usage: $0 {validate|train|predict} {dev|prod} [gcloud execute flags]" >&2
  exit 2
fi

project_variable="$(printf '%s_GCP_PROJECT_ID' "${env_name}" | tr '[:lower:]' '[:upper:]')"
project_id="${!project_variable:-${GCP_PROJECT_ID:-}}"
region="${GCP_REGION:-us-central1}"
if [[ -z "${project_id}" ]]; then
  echo "${project_variable} or GCP_PROJECT_ID is required" >&2
  exit 2
fi

gcloud run jobs execute "firmaware-${env_name}-${job}" \
  --project "${project_id}" \
  --region "${region}" \
  --wait \
  "$@"
