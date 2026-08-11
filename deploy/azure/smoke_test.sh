#!/usr/bin/env bash
# Prove the deployment is actually serving before any traffic reaches it.
#
# Two assertions matter more than the rest and are the reason this script exists
# rather than a plain health ping:
#
#   * Exactly one of the five fixture rows carries a vendor that is not in the
#     training corpus, and its `unseen_categories` must come back non-empty. A
#     model that silently encodes an unknown vendor as the baseline category
#     answers 200 with a confident, wrong number -- which is indistinguishable
#     from a correct answer unless something checks for it here.
#   * A deliberately malformed record must come back 422 naming the violation.
#     A serving stack that turns contract failures into defaults is worse than
#     one that is down, because nothing downstream will notice.
#
# Requests are addressed to the deployment-specific route via the
# azureml-model-deployment header, so this tests the new deployment even while
# it holds 0% of the endpoint's traffic. Passing an empty deployment drops the
# header and routes through the endpoint's traffic map instead, which is what
# promote_traffic.sh needs between steps: only that path proves real callers are
# being served correctly.
set -euo pipefail

env_name="${1:?usage: smoke_test.sh <dev|prod> [deployment]}"
# ${VAR-default} rather than ${VAR:-default}: an explicitly empty value means
# "route through the endpoint" and must not fall through to a deployment name.
deployment="${2-${FIRMAWARE_DEPLOYMENT-green}}"
endpoint="firmaware-score-${env_name}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
expected_version="${FIRMAWARE_MODEL_VERSION:-}"
image_digest="${FIRMAWARE_IMAGE_DIGEST:-unknown}"
fixture="${FIRMAWARE_SMOKE_FIXTURE:-deploy/azure/fixtures/upcoming_smoke.csv}"

route_headers=()
if [[ -n "${deployment}" ]]; then
  route_headers=(-H "azureml-model-deployment: ${deployment}")
  route_label="deployment ${deployment}"
else
  route_label="endpoint traffic map"
fi

fail() { echo "SMOKE FAIL: $1" >&2; exit 1; }

scoring_uri="$(az ml online-endpoint show --name "${endpoint}" \
  --workspace-name "${workspace}" --resource-group "${resource_group}" \
  --query scoring_uri -o tsv)"

# AAD token, not an endpoint key: there is no key to leak, and the token is
# scoped to the caller's own identity for the audit trail.
token="$(az account get-access-token --resource https://ml.azure.com \
  --query accessToken -o tsv)"

payload="$(python3 -c '
import csv, json, sys
with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
print(json.dumps({"records": rows}))
' "${fixture}")"

response_file="$(mktemp)"
trap 'rm -f "${response_file}"' EXIT

status="$(curl -sS -o "${response_file}" -w '%{http_code}' \
  -X POST "${scoring_uri}" \
  -H "Authorization: Bearer ${token}" \
  -H "Content-Type: application/json" \
  "${route_headers[@]+"${route_headers[@]}"}" \
  --data-binary "${payload}")"

[[ "${status}" == "200" ]] || fail "expected 200 via ${route_label}, got ${status}: $(cat "${response_file}")"

python3 - "${response_file}" "${expected_version}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    body = json.load(handle)
expected_version = sys.argv[2]

results = body.get("results")
if not isinstance(results, list):
    raise SystemExit("SMOKE FAIL: response has no results array")
if len(results) != 5:
    raise SystemExit(f"SMOKE FAIL: expected 5 results, got {len(results)}")

required = {
    "risk_probability",
    "risk_prediction",
    "risk_band",
    "unseen_categories",
    "model_version",
    "threshold",
}
for index, row in enumerate(results):
    missing = required - set(row)
    if missing:
        raise SystemExit(f"SMOKE FAIL: row {index} missing {sorted(missing)}")
    if row["risk_prediction"] not in {"GO", "NO_GO"}:
        raise SystemExit(f"SMOKE FAIL: row {index} bad decision {row['risk_prediction']!r}")

unseen = [row for row in results if row["unseen_categories"]]
if len(unseen) != 1:
    raise SystemExit(
        "SMOKE FAIL: expected exactly 1 row with unseen categories, got "
        f"{len(unseen)}; the out-of-corpus vendor is being silently encoded"
    )

versions = {str(row["model_version"]) for row in results}
if len(versions) != 1:
    raise SystemExit(f"SMOKE FAIL: mixed model versions in one response: {versions}")
served = versions.pop()
if expected_version and served != expected_version:
    raise SystemExit(
        f"SMOKE FAIL: serving model version {served}, expected {expected_version}"
    )

thresholds = {row["threshold"] for row in results}
if len(thresholds) != 1 or None in thresholds:
    raise SystemExit(f"SMOKE FAIL: threshold not reported consistently: {thresholds}")

print(f"[smoke] 5/5 scored, model_version={served}, threshold={thresholds.pop()}")
print(f"[smoke] unseen categories surfaced on: {unseen[0]['deployment_id']}")
PY

# A record that violates the scoring contract. site_criticality is a closed
# vocabulary, so this is a named violation rather than a type error, and the
# response has to say so.
malformed='{"records":[{"deployment_id":"smoke-bad","vendor_name":"Siemens","site_criticality":"NOT_A_LEVEL"}]}'
status="$(curl -sS -o "${response_file}" -w '%{http_code}' \
  -X POST "${scoring_uri}" \
  -H "Authorization: Bearer ${token}" \
  -H "Content-Type: application/json" \
  "${route_headers[@]+"${route_headers[@]}"}" \
  --data-binary "${malformed}")"

[[ "${status}" == "422" ]] \
  || fail "malformed record returned ${status}, expected 422: $(cat "${response_file}")"

message="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("error", {}).get("message", ""))
' "${response_file}")"
[[ -n "${message}" ]] || fail "422 carried no violation message"

echo "[smoke] contract violation correctly rejected: ${message}"
echo "[smoke] route=${route_label} image_digest=${image_digest}"
echo "[smoke] all assertions passed for ${env_name}"
