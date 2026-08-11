#!/usr/bin/env bash
# Staged traffic promotion with a health assertion between each step.
#
# 10 -> 50 -> 100, and the smoke test runs against the *endpoint* route between
# steps rather than the deployment-specific route. That distinction is the whole
# point: a deployment-targeted probe would pass even if the endpoint's traffic
# map were wrong, so the only thing that proves real users are being served
# correctly is a request that goes through the same routing they do.
#
# Any failure rolls the whole thing back to the previously live slot and exits
# non-zero. There is no partial success state to reason about at 3am.
set -euo pipefail

env_name="${1:?usage: promote_traffic.sh <dev|prod> <target> [live]}"
target="${2:-${FIRMAWARE_DEPLOYMENT:-green}}"
live="${3:-$([[ "${target}" == "green" ]] && echo blue || echo green)}"
endpoint="firmaware-score-${env_name}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
settle_seconds="${FIRMAWARE_SETTLE_SECONDS:-30}"

if [[ "${target}" == "${live}" ]]; then
  echo "target and live deployment are both ${target}" >&2
  exit 2
fi

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

rollback() {
  echo "[promote] health check failed at ${1}% - rolling back to ${live}" >&2
  bash deploy/azure/rollback_endpoint.sh "${env_name}" "${live}"
  exit 1
}

# Verified before any traffic moves: an unhealthy slot must never receive even
# 10% of production requests.
state="$(az_ml online-deployment show --endpoint-name "${endpoint}" --name "${target}" \
  --query provisioning_state -o tsv)"
[[ "${state}" == "Succeeded" ]] || { echo "${target} is ${state}, not Succeeded" >&2; exit 1; }

# On the very first release only one deployment exists, and the endpoint rejects
# a traffic map naming a deployment it does not have. Checking is cheaper than
# discovering it during the first production promotion.
existing="$(az_ml online-deployment list --endpoint-name "${endpoint}" \
  --query "[].name" -o tsv)"
live_exists=0
if grep -qx "${live}" <<< "${existing}"; then
  live_exists=1
else
  echo "[promote] ${live} does not exist yet; ${target} takes 100% directly"
fi

for share in 10 50 100; do
  if [[ "${live_exists}" -eq 1 ]]; then
    az_ml online-endpoint update --name "${endpoint}" \
      --traffic "${live}=$((100 - share)) ${target}=${share}"
  else
    # Nothing to split against: a first deployment goes straight to 100 and the
    # staged loop would just be three identical updates.
    az_ml online-endpoint update --name "${endpoint}" --traffic "${target}=100"
    share=100
  fi
  echo "[promote] ${target}=${share}%"

  # Managed endpoints apply a traffic change asynchronously; probing immediately
  # would test the old split and pass for the wrong reason.
  sleep "${settle_seconds}"

  # An explicitly empty FIRMAWARE_DEPLOYMENT means "route through the endpoint's
  # traffic map" rather than "probe a named slot". smoke_test.sh reads it with
  # ${VAR-default}, so empty and unset are deliberately different here: only an
  # endpoint-routed probe proves real callers are being served correctly.
  #
  # FIRMAWARE_MODEL_VERSION is deliberately cleared below 100%. At a 90/10 split
  # a single request lands on the OLD slot about nine times in ten, and the old
  # slot legitimately serves the previous version -- asserting the new version
  # there would roll back almost every release for a reason that is not a fault.
  # The partial steps assert liveness and contract; the exact-version assertion
  # is meaningful only once the new slot owns all the traffic.
  if [[ "${share}" -eq 100 ]]; then
    # shellcheck disable=SC1007
    FIRMAWARE_DEPLOYMENT= bash deploy/azure/smoke_test.sh "${env_name}" "" || rollback "${share}"
  else
    # shellcheck disable=SC1007
    FIRMAWARE_DEPLOYMENT= FIRMAWARE_MODEL_VERSION= \
      bash deploy/azure/smoke_test.sh "${env_name}" "" || rollback "${share}"
  fi

  [[ "${live_exists}" -eq 1 ]] || break
done

if [[ "${live_exists}" -eq 1 ]]; then
  echo "[promote] ${target} is serving 100%; ${live} retained at 0% for rollback"
else
  echo "[promote] ${target} is serving 100%; no previous slot to retain"
fi
