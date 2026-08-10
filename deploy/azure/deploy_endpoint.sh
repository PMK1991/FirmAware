#!/usr/bin/env bash
# Create or update the idle deployment slot at ZERO traffic.
#
# The slot is chosen, not assumed. Whichever of blue/green currently holds less
# traffic is the idle one, and that is what gets replaced -- so a second release
# in a row does not overwrite the deployment that is actually serving. The names
# alternate; "green is new" is a convention that breaks on the second deploy.
#
# Nothing here shifts traffic. The new slot is fully provisioned and smoke-tested
# while the live slot keeps 100%, and promote_traffic.sh is a separate, explicit
# act. That separation is what makes rollback a traffic update rather than a
# redeploy, and it is why the rollback target is under 30 seconds.
set -euo pipefail

env_name="${1:?usage: deploy_endpoint.sh <dev|prod> <model_version> <image_digest>}"
model_version="${2:?model version is required: deploy the version that was tested}"
image_digest="${3:?image digest is required: tags are mutable, digests are not}"

endpoint="firmaware-score-${env_name}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
instance_type="${FIRMAWARE_INSTANCE_TYPE:-Standard_DS3_v2}"
min_instances="${FIRMAWARE_MIN_INSTANCES:-0}"
mlflow_uri="${MLFLOW_TRACKING_URI:-}"

# The six mandatory tags are enforced by a Deny policy at resource-group scope,
# so a deployment missing any of them is rejected before it is created. These
# mirror the values Terraform sets; env-scoped ones default per environment.
owner="${FIRMAWARE_OWNER:-data4v}"
cost_center="${FIRMAWARE_COST_CENTER:-firmaware-$([[ "${env_name}" == "prod" ]] && echo prod || echo rnd)}"
data_classification="${FIRMAWARE_DATA_CLASSIFICATION:-$([[ "${env_name}" == "prod" ]] && echo confidential || echo internal)}"
managed_by="${FIRMAWARE_MANAGED_BY:-azure-cli}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi
if [[ ! "${image_digest}" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "image_digest must be a full <registry>/<repo>@sha256:<64 hex> reference" >&2
  exit 2
fi

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

# The traffic map is the source of truth for which slot is live. An endpoint with
# no deployments yet returns an empty map, in which case blue is simply first.
traffic_json="$(az_ml online-endpoint show --name "${endpoint}" --query traffic -o json)"
target="$(python3 -c '
import json, sys
traffic = json.loads(sys.argv[1] or "{}") or {}
if not traffic:
    # A fresh endpoint has no deployments at all. Land on blue so the slot the
    # spec calls the retained fallback is the one that exists first.
    print("blue")
else:
    blue = int(traffic.get("blue", 0))
    green = int(traffic.get("green", 0))
    # Replace the slot carrying the least traffic; ties go to green so a
    # redeploy at 100/0 does not overwrite the slot that is serving.
    print("green" if green <= blue else "blue")
' "${traffic_json}")"

live="$([[ "${target}" == "green" ]] && echo blue || echo green)"
echo "[deploy] live slot: ${live}; deploying into: ${target}"

if [[ -z "${mlflow_uri}" ]]; then
  mlflow_uri="$(az ml workspace show --name "${workspace}" \
    --resource-group "${resource_group}" --query mlflow_tracking_uri -o tsv)"
fi

# Rendered INTO the spec directory, not /tmp. The az ml CLI resolves relative
# paths inside a YAML spec against the spec file's own location, so a temp file
# elsewhere would make `code: .` point at /tmp and upload the wrong directory --
# or fail outright when score.py is not found there.
rendered="$(mktemp -p deploy/azure/azureml --suffix=.yaml)"
trap 'rm -f "${rendered}"' EXIT

# Substitution rather than a templating engine: the deployment YAML stays a
# readable, reviewable artefact in the repo, and the only things that vary
# between environments are the values injected here.
MODEL_VERSION="${model_version}" \
IMAGE_DIGEST="${image_digest}" \
ENV="${env_name}" \
INSTANCE_TYPE="${instance_type}" \
MIN_INSTANCES="${min_instances}" \
WORKSPACE_MLFLOW_URI="${mlflow_uri}" \
OWNER="${owner}" \
COST_CENTER="${cost_center}" \
DATA_CLASSIFICATION="${data_classification}" \
MANAGED_BY="${managed_by}" \
python3 -c '
import os
import re
import sys

source, destination = sys.argv[1], sys.argv[2]
with open(source, encoding="utf-8") as handle:
    text = handle.read()

def replace(match):
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(f"unsubstituted placeholder in deployment YAML: {name}")
    return value

text = re.sub(r"\$\{\{([A-Z_]+)\}\}", replace, text)
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(text)
' "deploy/azure/azureml/deployment-${target}.yaml" "${rendered}"

# Idempotent: `az ml online-deployment create` updates an existing deployment of
# the same name, so a rerun of a failed release converges rather than conflicts.
# --all-traffic is deliberately not passed: omitting it leaves the traffic map
# alone, which is the entire contract of this script.
az_ml online-deployment create --file "${rendered}" --name "${target}"

# The endpoint's traffic map must be untouched by this script. Asserting it here
# turns a silent CLI behaviour change into a loud failure before smoke testing.
after="$(az_ml online-endpoint show --name "${endpoint}" --query traffic -o json)"
python3 -c '
import json, sys
before = json.loads(sys.argv[1] or "{}") or {}
after = json.loads(sys.argv[2] or "{}") or {}
target = sys.argv[3]
if before and after.get(target, 0) != before.get(target, 0):
    raise SystemExit(
        f"deploy shifted traffic to {target}: {before} -> {after}; "
        "the new slot must stay at 0% until smoke passes"
    )
' "${traffic_json}" "${after}" "${target}"

echo "deployment=${target}"
echo "live_deployment=${live}"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "deployment=${target}"
    echo "live_deployment=${live}"
  } >> "${GITHUB_OUTPUT}"
fi
echo "[deploy] ${target} is provisioned at 0% traffic, model version ${model_version}"
