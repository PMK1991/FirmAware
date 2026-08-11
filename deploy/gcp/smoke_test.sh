#!/usr/bin/env bash
set -euo pipefail

env_name="${1:-}"
if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Usage: $0 {dev|prod}" >&2
  exit 2
fi

project_variable="$(printf '%s_GCP_PROJECT_ID' "${env_name}" | tr '[:lower:]' '[:upper:]')"
project_id="${!project_variable:-${GCP_PROJECT_ID:-}}"
region="${GCP_REGION:-us-central1}"
data_bucket="firmaware-${env_name}-data"
artifacts_bucket="firmaware-${env_name}-artifacts"
scores_bucket="firmaware-${env_name}-scores"
job_name="firmaware-${env_name}-predict"

if [[ -z "${project_id}" ]]; then
  echo "${project_variable} or GCP_PROJECT_ID is required" >&2
  exit 2
fi

report_failure() {
  exit_code=$?
  trap - ERR
  set +e
  gcloud logging write firmaware_smoke_test \
    "{\"environment\":\"${env_name}\",\"exitCode\":1,\"job\":\"${job_name}\",\"message\":\"FirmAware smoke test failed\",\"sourceExitCode\":${exit_code}}" \
    --project "${project_id}" \
    --payload-type=json \
    --severity=ERROR >/dev/null 2>&1
  exit "${exit_code}"
}
trap report_failure ERR

before="$(mktemp)"
after="$(mktemp)"
score_file="$(mktemp)"
metadata_file="$(mktemp)"
trap 'rm -f "${before}" "${after}" "${score_file}" "${metadata_file}"' EXIT

gcloud storage ls "gs://${scores_bucket}/scores/**" 2>/dev/null | tr -d '\r' | sort > "${before}" || true
gcloud run jobs execute "${job_name}" \
  --project "${project_id}" \
  --region "${region}" \
  --args="predict,--input,gs://${data_bucket}/smoke/upcoming_smoke.csv" \
  --wait
gcloud storage ls "gs://${scores_bucket}/scores/**" | tr -d '\r' | sort > "${after}"

new_object="$(comm -13 "${before}" "${after}" | tail -n 1)"
if [[ -z "${new_object}" ]]; then
  echo "Smoke prediction did not create a new scores object" >&2
  false
fi

gcloud storage cp "${new_object}" "${score_file}" --quiet
pointer="$(gcloud storage cat "gs://${artifacts_bucket}/champion.json")"
run_id="$(python -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<< "${pointer}")"
gcloud storage cp \
  "gs://${artifacts_bucket}/runs/${run_id}/metadata.json" \
  "${metadata_file}" \
  --quiet

python - "${score_file}" "${metadata_file}" <<'PY'
import json
import sys

import pandas as pd

scores = pd.read_csv(sys.argv[1])
metadata = json.load(open(sys.argv[2], encoding="utf-8"))
if len(scores) != 5:
    raise SystemExit(f"expected 5 smoke rows, found {len(scores)}")
if int(scores["unseen_categories"].ne("{}").sum()) != 1:
    raise SystemExit("expected exactly one unseen-category smoke row")
if set(scores["model_run"]) != {metadata["timestamp"]}:
    raise SystemExit("smoke scores do not use the champion model timestamp")
PY

image="$(gcloud run jobs describe "${job_name}" \
  --project "${project_id}" \
  --region "${region}" \
  --format='value(spec.template.spec.template.spec.containers[0].image)')"
echo "smoke_scores_object=${new_object}"
echo "champion_run_id=${run_id}"
echo "image_digest=${image}"
