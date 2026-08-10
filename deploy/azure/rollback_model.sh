#!/usr/bin/env bash
# Batch model rollback: re-point the batch endpoint's default deployment at an
# earlier registry version.
#
# Batch cannot roll back the way online does. There is no warm second slot to
# flip traffic to, so this creates (or reuses) a deployment pinned to the
# requested version and makes it the default. That is why the architecture gives
# it a five-minute target rather than the online path's thirty seconds.
#
# Scores already written are never touched. They are append-only evidence under a
# platform immutability policy: a superseded score stays, and the corrected run
# is written alongside it. Rolling back the model does not rewrite history.
set -euo pipefail

env_name="${1:?usage: rollback_model.sh <dev|prod> <model_version>}"
model_version="${2:?model version is required}"
endpoint="firmaware-batch-${env_name}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
image_digest="${FIRMAWARE_IMAGE_DIGEST:-unknown}"
model_name="${FIRMAWARE_REGISTERED_MODEL_NAME:-FirmAwareRiskModel}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi
if [[ ! "${model_version}" =~ ^[0-9]+$ ]]; then
  echo "Model version must be a registry version number" >&2
  exit 2
fi

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

# Refuse to roll back to something that does not exist. Without this the CLI
# would create a deployment that fails at first invocation, during an incident.
az_ml model show --name "${model_name}" --version "${model_version}" >/dev/null \
  || { echo "model ${model_name}:${model_version} is not in the registry" >&2; exit 1; }

deployment="batch-${model_version}"
# Rendered into the spec directory: the az ml CLI resolves the spec's relative
# `code:` path against the file's own location, so a temp file in /tmp would
# upload /tmp instead of the scoring script.
rendered="$(mktemp -p deploy/azure/azureml --suffix=.yaml)"
trap 'rm -f "${rendered}"' EXIT

MODEL_VERSION="${model_version}" \
IMAGE_DIGEST="${image_digest}" \
ENV="${env_name}" \
OWNER="${FIRMAWARE_OWNER:-data4v}" \
COST_CENTER="${FIRMAWARE_COST_CENTER:-firmaware-$([[ "${env_name}" == "prod" ]] && echo prod || echo rnd)}" \
DATA_CLASSIFICATION="${FIRMAWARE_DATA_CLASSIFICATION:-$([[ "${env_name}" == "prod" ]] && echo confidential || echo internal)}" \
MANAGED_BY="${FIRMAWARE_MANAGED_BY:-azure-cli}" \
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

with open(destination, "w", encoding="utf-8") as handle:
    handle.write(re.sub(r"\$\{\{([A-Z_]+)\}\}", replace, text))
' deploy/azure/azureml/deployment-batch.yaml "${rendered}"

if az_ml batch-deployment show --endpoint-name "${endpoint}" --name "${deployment}" >/dev/null 2>&1; then
  echo "[rollback] ${deployment} already exists, reusing it"
else
  az_ml batch-deployment create --endpoint-name "${endpoint}" --file "${rendered}"
fi

az_ml batch-endpoint update --name "${endpoint}" --defaults deployment_name="${deployment}"

current="$(az_ml batch-endpoint show --name "${endpoint}" \
  --query defaults.deployment_name -o tsv)"
[[ "${current}" == "${deployment}" ]] \
  || { echo "default deployment is ${current}, expected ${deployment}" >&2; exit 1; }

echo "[rollback] batch endpoint ${endpoint} now defaults to ${model_name}:${model_version}"
echo "[rollback] previously written scores are unchanged and remain immutable"
