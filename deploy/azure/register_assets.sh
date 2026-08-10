#!/usr/bin/env bash
# Register the AML environment and the versioned data assets the pipeline needs.
#
# This exists because every component and deployment YAML refers to
# `azureml:firmaware-env@latest`, and the pipeline resolves
# `azureml:firmaware-deployment-events@latest` and `azureml:firmaware-config@latest`.
# Terraform creates the workspace, the compute and the endpoints, but AML assets
# are workspace *content* rather than infrastructure -- they version with the
# code and the image, not with the resource group -- so they are registered here,
# after the image exists and before anything references them.
#
# Idempotent by design. Registering an identical environment or data asset again
# is a no-op that returns the existing version, so re-running a failed deploy
# does not inflate the version history and does not change what @latest means.
set -euo pipefail

env_name="${1:?usage: register_assets.sh <dev|prod>}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
storage_account="${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"
image_digest="${FIRMAWARE_IMAGE_DIGEST:?FIRMAWARE_IMAGE_DIGEST is required}"
registry="${AZURE_ACR_NAME:?AZURE_ACR_NAME is required}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

# The digest may arrive either bare (sha256:...) or fully qualified
# (registry/repo@sha256:...). Both are accepted; only the digest part is used,
# because the login server is resolved from the registry rather than trusted
# from the caller.
digest="${image_digest##*@}"
if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "FIRMAWARE_IMAGE_DIGEST must contain a sha256 digest, got: ${image_digest}" >&2
  exit 2
fi

login_server="$(az acr show --name "${registry}" --query loginServer -o tsv)"

echo "[assets] environment firmaware-env -> ${login_server}/firmaware@${digest}"

rendered="$(mktemp --suffix=.yaml)"
trap 'rm -f "${rendered}"' EXIT

ACR_LOGIN_SERVER="${login_server}" \
IMAGE_DIGEST="${digest}" \
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
        raise SystemExit(f"unsubstituted placeholder in environment YAML: {name}")
    return value

with open(destination, "w", encoding="utf-8") as handle:
    handle.write(re.sub(r"\$\{\{([A-Z_]+)\}\}", replace, text))
' deploy/azure/azureml/environment.yaml "${rendered}"

# No `code:` in environment.yaml, so rendering to /tmp is safe here -- unlike the
# deployment specs, which must render beside the scoring script they upload.
az_ml environment create --file "${rendered}"

environment_version="$(az_ml environment list --name firmaware-env --query "[].version" -o tsv \
  | sort -n | tail -1)"
echo "[assets] firmaware-env version ${environment_version}"

# --- data assets --------------------------------------------------------------
# Two different kinds of asset, registered two different ways on purpose.
#
# The raw events file is platform data: it already lives in the storage account
# under an immutability policy, so the asset is a versioned *pointer* at it.
# Uploading would make a second copy that could drift from the one the batch
# scorer reads.
#
# The config is source: it lives in git, changes with the code, and is uploaded
# so the workspace holds the exact bytes the run used. Its version is the hash of
# its own content, which makes re-registration idempotent and makes "which config
# produced this model" answerable from the version string alone.
data_container="${FIRMAWARE_DATA_CONTAINER:-data}"
data_root="abfss://${data_container}@${storage_account}.dfs.core.windows.net"

register_pointer() {
  local name="$1" path="$2" description="$3"
  # A new version is cut only when the pointer actually moves. Re-running a
  # failed deploy must not inflate the version history, because the version is
  # what a model's provenance tag refers to.
  if az_ml data show --name "${name}" --label latest >/dev/null 2>&1; then
    local current
    current="$(az_ml data show --name "${name}" --label latest --query path -o tsv)"
    if [[ "${current}" == "${path}" ]]; then
      echo "[assets] ${name} already points at ${path}"
      return 0
    fi
    echo "[assets] ${name} moves from ${current} to ${path}; cutting a new version"
  fi
  az_ml data create \
    --name "${name}" \
    --type uri_file \
    --path "${path}" \
    --description "${description}" \
    --output none
  local version
  version="$(az_ml data list --name "${name}" --query "[].version" -o tsv | sort -n | tail -1)"
  echo "[assets] ${name} version ${version} -> ${path}"
}

register_file() {
  local name="$1" source_path="$2" description="$3"
  local content_hash current_hash
  content_hash="$(python3 -c '
import hashlib
import sys

with open(sys.argv[1], "rb") as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
' "${source_path}")"

  # Compared against what @latest currently resolves to, not against "does a
  # version with this hash exist anywhere". AML resolves @latest by creation
  # time, so a revert to earlier content would otherwise find its hash already
  # registered, create nothing, and leave @latest pointing at the content that
  # was just reverted away from -- training would then run with a config the
  # repository no longer contains, which is the opposite of reproducible.
  current_hash="$(az_ml data show --name "${name}" --label latest \
    --query "tags.content_sha256" -o tsv 2>/dev/null || true)"

  if [[ "${current_hash}" == "${content_hash}" ]]; then
    echo "[assets] ${name}@latest already holds this content (${content_hash:0:12})"
    return 0
  fi

  # Auto-versioned rather than named after the hash: a revert has to produce a
  # NEW version, and a hash-named version cannot be created twice.
  az_ml data create \
    --name "${name}" \
    --type uri_file \
    --path "${source_path}" \
    --description "${description}" \
    --tags content_sha256="${content_hash}" \
    --output none
  local version
  version="$(az_ml data show --name "${name}" --label latest --query version -o tsv)"
  echo "[assets] ${name} version ${version} <- ${source_path} (${content_hash:0:12})"
}

register_pointer firmaware-deployment-events \
  "${data_root}/deployment_events.csv" \
  "Raw firmware deployment events. Versioned so a model version records the exact data it saw."

register_file firmaware-config \
  deploy/azure/azureml/config.azure.yaml \
  "Pipeline configuration. A new version is cut whenever its content changes, including a revert."

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "environment_version=${environment_version}" >> "${GITHUB_OUTPUT}"
fi

echo "[assets] registration complete for ${env_name}"
