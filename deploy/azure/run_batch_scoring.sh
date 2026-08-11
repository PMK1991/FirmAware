#!/usr/bin/env bash
# Run a batch scoring job and publish its results as immutable evidence.
#
# Two steps, because one is not possible. The batch endpoint scores onto the
# workspace store, which is scratch, and a second job promotes that output into
# the append-only scores container. The endpoint cannot write there itself:
# `--output-path` refuses an ADLS Gen2 datastore, and registering the same
# container over the blob API gets as far as HTTP 409 "blob is immutable due to
# a policy" -- the container permits append-style writes and AML uploads whole
# blobs. See job-publish-scores.yaml.
#
# The staging path is run-unique so two concurrent runs cannot interleave rows
# into one another's output, and so a re-run never reads a previous run's file.
set -euo pipefail

env_name="${1:?usage: run_batch_scoring.sh <dev|prod> <input_uri_or_path>}"
input="${2:?an input file is required}"
endpoint="firmaware-batch-${env_name}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
storage_account="${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"
scores_container="${FIRMAWARE_SCORES_CONTAINER:-scores}"
staging_datastore="${FIRMAWARE_STAGING_DATASTORE:-workspaceblobstore}"
timeout_seconds="${FIRMAWARE_BATCH_TIMEOUT:-2400}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

az_ml() { az ml "$@" --workspace-name "${workspace}" --resource-group "${resource_group}"; }

# Polled rather than `az ml job stream`: a batch run spends most of its time
# waiting for the scale-to-zero cluster, and a CI runner that drops the stream
# would lose the job with it.
wait_for() {
  local job="$1" label="$2" status=""
  local deadline=$(( $(date +%s) + timeout_seconds ))
  while [[ "$(date +%s)" -lt "${deadline}" ]]; do
    status="$(az_ml job show -n "${job}" --query status -o tsv)"
    case "${status}" in
      Completed) return 0 ;;
      Failed | Canceled)
        echo "[batch] ${label} job ${job} finished ${status}" >&2
        return 1
        ;;
    esac
    sleep 30
  done
  echo "[batch] ${label} job ${job} still ${status} after ${timeout_seconds}s" >&2
  return 1
}

run_id="run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
staging_path="azureml://datastores/${staging_datastore}/paths/batch-staging/${run_id}"
scores_uri="abfss://${scores_container}@${storage_account}.dfs.core.windows.net"

echo "[batch] scoring ${input} on ${endpoint}"
job="$(az_ml batch-endpoint invoke \
  --name "${endpoint}" \
  --input "${input}" \
  --input-type uri_file \
  --output-path "${staging_path}" \
  --query name -o tsv)"
[[ -n "${job}" ]] || { echo "[batch] invoke returned no job name" >&2; exit 1; }
echo "[batch] scoring job ${job} -> ${staging_path}"

wait_for "${job}" scoring

echo "[batch] publishing ${staging_path} to ${scores_uri}"

# The cluster's own identity, so the publish job authenticates as the principal
# that actually holds the append-only role on the scores container. Read from
# the compute rather than configured, so dev and prod cannot drift apart and a
# rebuilt cluster needs no edit here.
cluster="${FIRMAWARE_COMPUTE_NAME:-firmaware-cluster}"
client_id="$(az_ml compute show --name "${cluster}" \
  --query "identity.user_assigned_identities[0].client_id" -o tsv)"
[[ -n "${client_id}" && "${client_id}" != "None" ]] \
  || { echo "[batch] ${cluster} has no user-assigned identity to authenticate as" >&2; exit 1; }

# Rendered into the spec's own directory: the CLI resolves a relative `code:`
# against the spec file's location, so a temp file elsewhere makes it look for
# the step scripts under /tmp. `--set code=` does not help -- that value is
# relative too, and is resolved the same way.
rendered="$(mktemp -p deploy/azure/azureml --suffix=.yaml)"
trap 'rm -f "${rendered}"' EXIT

# The renderer is single-quoted on purpose: it contains `${...}` only as literal
# text it must not have expanded, and the shell values it needs arrive through
# the environment assignments below and through argv. The disable must sit here
# rather than beside `python3`, because a comment between a trailing backslash
# and the command breaks the continuation -- the assignments then form their own
# no-op command and the renderer sees none of them.
# shellcheck disable=SC2016
STAGED_PATH="${staging_path}" \
SCORES_URI="${scores_uri}" \
SOURCE_JOB="${job}" \
CLIENT_ID="${client_id}" \
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
        raise SystemExit(f"unsubstituted placeholder in publish job YAML: {name}")
    return value

# Only fully upper-case placeholders are substituted. ${{inputs.staged}} and the
# other AML bindings in the command must survive untouched.
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(re.sub(r"\$\{\{([A-Z_]+)\}\}", replace, text))
' deploy/azure/azureml/job-publish-scores.yaml "${rendered}"

publish_job="$(az_ml job create --file "${rendered}" --query name -o tsv)"
[[ -n "${publish_job}" ]] || { echo "[batch] publish job was not created" >&2; exit 1; }
echo "[batch] publish job ${publish_job}"

wait_for "${publish_job}" publish

echo "scoring_job=${job}"
echo "publish_job=${publish_job}"
echo "staging_path=${staging_path}"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "scoring_job=${job}"
    echo "publish_job=${publish_job}"
    echo "staging_path=${staging_path}"
  } >> "${GITHUB_OUTPUT}"
fi
echo "[batch] scores published to ${scores_uri}"
