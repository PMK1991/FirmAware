#!/usr/bin/env bash
# Model rollback: a traffic flip, nothing else.
#
# The previous slot was never torn down and is still warm, so this is a single
# control-plane call and no container start. That is the entire reason the
# architecture retains both deployments instead of replacing one in place, and
# it is what makes the sub-30-second target achievable rather than aspirational.
#
# The slot to roll back to is resolved from the endpoint rather than assumed to
# be blue: after an even number of releases the fallback is green, and an
# incident is the worst moment to discover a hardcoded name was wrong.
set -euo pipefail

env_name="${1:?usage: rollback_endpoint.sh <dev|prod> [target]}"
endpoint="firmaware-score-${env_name}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

target="${2:-}"
if [[ -z "${target}" ]]; then
  traffic_json="$(az_ml online-endpoint show --name "${endpoint}" --query traffic -o json)"
  target="$(python3 -c '
import json, sys
traffic = json.loads(sys.argv[1] or "{}") or {}
blue = int(traffic.get("blue", 0))
green = int(traffic.get("green", 0))
# Roll back to whichever slot is not currently taking the traffic.
print("blue" if green >= blue else "green")
' "${traffic_json}")"
fi

other="$([[ "${target}" == "green" ]] && echo blue || echo green)"

# Fail loudly rather than route traffic into a slot that is not serving.
state="$(az_ml online-deployment show --endpoint-name "${endpoint}" --name "${target}" \
  --query provisioning_state -o tsv 2>/dev/null || echo "Missing")"
[[ "${state}" == "Succeeded" ]] \
  || { echo "cannot roll back: ${target} is ${state}" >&2; exit 1; }

# The endpoint rejects a traffic map naming a deployment it does not have, so the
# other slot is only named if it exists. On the first release there is nothing to
# roll back *to*, but a rollback can still be invoked by the deploy job's failure
# handler, and it must not fail for a second reason on top of the first.
if az_ml online-deployment show --endpoint-name "${endpoint}" --name "${other}" \
  >/dev/null 2>&1; then
  traffic="${target}=100 ${other}=0"
else
  traffic="${target}=100"
fi

az_ml online-endpoint update --name "${endpoint}" --traffic "${traffic}"
after="$(az_ml online-endpoint show --name "${endpoint}" --query "traffic.${target}" -o tsv)"
[[ "${after}" == "100" ]] || { echo "rollback did not take: ${target}=${after}" >&2; exit 1; }

echo "[rollback] ${target} is serving 100%; traffic map is now: ${traffic}"
