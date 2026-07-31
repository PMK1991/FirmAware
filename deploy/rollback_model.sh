#!/usr/bin/env bash
set -euo pipefail

env_name="${1:-}"
run_id="${2:-}"
if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Usage: $0 {dev|prod} [run_id]" >&2
  exit 2
fi

artifacts_bucket="firmaware-${env_name}-artifacts"
if [[ -z "${run_id}" ]]; then
  echo "Available immutable model runs:"
  while IFS= read -r run_uri; do
    run_uri="${run_uri%$'\r'}"
    candidate="${run_uri%/}"
    candidate="${candidate##*/}"
    metadata="$(gcloud storage cat \
      "gs://${artifacts_bucket}/runs/${candidate}/metadata.json" 2>/dev/null || true)"
    if [[ -n "${metadata}" ]]; then
      python -c 'import json,sys
metadata=json.load(sys.stdin)
metrics=metadata.get("metrics",{}).get(metadata.get("model_name"),{})
print("{} model={} cost={} auc={} timestamp={}".format(sys.argv[1],metadata.get("model_name"),metrics.get("expected_cost"),metrics.get("roc_auc"),metadata.get("timestamp")))' \
        "${candidate}" <<< "${metadata}"
    fi
  done < <(gcloud storage ls "gs://${artifacts_bucket}/runs/")
  exit 0
fi
if [[ ! "${run_id}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "run_id contains unsupported characters" >&2
  exit 2
fi

metadata_file="$(mktemp)"
pointer_file="$(mktemp)"
trap 'rm -f "${metadata_file}" "${pointer_file}"' EXIT
gcloud storage cp \
  "gs://${artifacts_bucket}/runs/${run_id}/metadata.json" \
  "${metadata_file}" \
  --quiet
digest="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "${metadata_file}")"
promoted_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
promoted_by="${FIRMAWARE_PROMOTED_BY:-$(gcloud config get-value account 2>/dev/null)}"

python - "${run_id}" "${digest}" "${promoted_at}" "${promoted_by}" > "${pointer_file}" <<'PY'
import json
import sys

print(json.dumps({
    "run_id": sys.argv[1],
    "digest_of_metadata": sys.argv[2],
    "promoted_at": sys.argv[3],
    "promoted_by": sys.argv[4],
}, indent=2, sort_keys=True))
PY

pointer_uri="gs://${artifacts_bucket}/champion.json"
generation="$(gcloud storage objects describe "${pointer_uri}" \
  --format='value(generation)' 2>/dev/null || echo 0)"
gcloud storage cp \
  "${pointer_file}" \
  "${pointer_uri}" \
  --content-type=application/json \
  --if-generation-match="${generation}" \
  --quiet
echo "champion_run_id=${run_id}"
echo "metadata_digest=${digest}"
