#!/usr/bin/env bash
# Point the batch endpoint at a registry version, creating its deployment if it
# does not exist yet.
#
# This exists because the forward path needs exactly what rollback needs. A
# batch endpoint has no traffic map and no warm second slot: a deployment is
# either the endpoint's default or it is addressed by name, so "release" and
# "roll back" are the same operation aimed at different versions. Leaving that
# operation inside rollback_model.sh meant the first batch deployment of any
# environment could only be created by rolling back -- which is a strange thing
# to have to do to something that has never been deployed, and worse, it made
# the rollback path the one that had never been exercised at the moment an
# incident needed it.
#
# Deployments are named for the model version and kept, so previous versions
# stay individually addressable and re-pointing is a metadata change rather than
# a rebuild.
set -euo pipefail

env_name="${1:?usage: deploy_batch.sh <dev|prod> <model_version>}"
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

# Refuse to point at something that does not exist. Without this the CLI would
# create a deployment that fails at its first invocation -- during an incident,
# on the rollback path.
az_ml model show --name "${model_name}" --version "${model_version}" >/dev/null \
  || { echo "model ${model_name}:${model_version} is not in the registry" >&2; exit 1; }

deployment="batch-${model_version}"
# Rendered into the spec directory: the az ml CLI resolves the spec's relative
# `code:` path against the file's own location, so a temp file in /tmp would
# upload /tmp instead of the scoring script.
rendered="$(mktemp -p deploy/azure/azureml --suffix=.yaml)"
# The version, written where batch_score.py can read it. A batch deployment
# cannot carry environment variables -- the service accepts the field and
# discards it -- and nothing the runtime exposes names the registry version, so
# the code snapshot is the only carrier that is both per-deployment and
# immutable. Removed again afterwards so the working tree stays clean and a
# stale version can never be uploaded by a later, unrelated `code:` upload.
version_sidecar="deploy/azure/azureml/model_version.txt"
printf '%s\n' "${model_version}" > "${version_sidecar}"
trap 'rm -f "${rendered}" "${version_sidecar}"' EXIT

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

# Always created, never skipped when it already exists. `create` is an upsert
# here, and the deployment is named after the *model* version while its image,
# its scoring script and its environment all move independently of that version.
# Skipping the call when the name exists would therefore leave an unchanged
# deployment silently serving stale code -- on the rollback path, during an
# incident, which is exactly when that is least survivable.
az_ml batch-deployment create --endpoint-name "${endpoint}" --file "${rendered}"

az_ml batch-endpoint update --name "${endpoint}" --defaults deployment_name="${deployment}"

current="$(az_ml batch-endpoint show --name "${endpoint}" \
  --query defaults.deployment_name -o tsv)"
[[ "${current}" == "${deployment}" ]] \
  || { echo "default deployment is ${current}, expected ${deployment}" >&2; exit 1; }

echo "[batch] endpoint ${endpoint} now defaults to ${model_name}:${model_version}"
