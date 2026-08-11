#!/usr/bin/env bash
# Submit the Azure ML training pipeline and return the registry version it made.
#
# Training is a deliberate act and is never wired to merge: a training run
# changes model behaviour without changing a line of code, so it gets its own
# trigger and its own approval trail. Merging a refactor must not silently swap
# the model that is making deployment decisions.
#
# Provenance is passed in as pipeline inputs rather than read from the runner's
# environment inside the job, so the commit, image and data version are recorded
# on the job itself and a resubmission cannot quietly pick up different values.
set -euo pipefail

env_name="${1:?usage: run_training.sh <dev|prod>}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
git_sha="${FIRMAWARE_GIT_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"
image_digest="${FIRMAWARE_IMAGE_DIGEST:-unknown}"
model_name="${FIRMAWARE_REGISTERED_MODEL_NAME:-FirmAwareRiskModel}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

# The exact asset version the pipeline will resolve @latest to, captured now so
# the model version is tagged with the data it actually saw rather than with
# whatever @latest means when someone reads the tag months later.
data_version="$(az_ml data show --name firmaware-deployment-events --label latest \
  --query version -o tsv)"

run_id="$(az_ml job create \
  --file deploy/azure/azureml/pipeline-train.yaml \
  --set tags.env="${env_name}" \
  --set tags.git_sha="${git_sha}" \
  --set inputs.git_sha="${git_sha}" \
  --set inputs.image_digest="${image_digest}" \
  --set inputs.data_asset_version="${data_version}" \
  --query name -o tsv)"

echo "run_id=${run_id}"
echo "[train] data asset version ${data_version}, git ${git_sha}"

az_ml job stream --name "${run_id}"

status="$(az_ml job show --name "${run_id}" --query status -o tsv)"
if [[ "${status}" != "Completed" ]]; then
  # A gate failure lands here. That is the designed outcome, not a bug: the
  # registry has no new version and nothing will be deployed.
  echo "[train] pipeline ${status}; no model was registered" >&2
  exit 1
fi

# The registry is the interface. The version is read back from it rather than
# parsed out of job logs, so what is reported is what actually exists. Sorted
# numerically because registry versions are strings and "10" sorts before "9".
model_version="$(az_ml model list --name "${model_name}" --query "[].version" -o tsv \
  | sort -n | tail -1)"
[[ -n "${model_version}" ]] \
  || { echo "[train] pipeline completed but the registry has no version" >&2; exit 1; }

echo "model_version=${model_version}"
echo "mlflow_model=models:/${model_name}/${model_version}"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "run_id=${run_id}"
    echo "model_version=${model_version}"
    echo "data_asset_version=${data_version}"
  } >> "${GITHUB_OUTPUT}"
fi
