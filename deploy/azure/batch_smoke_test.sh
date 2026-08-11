#!/usr/bin/env bash
# Prove batch scoring works end to end, including that its output becomes
# immutable evidence rather than stopping at the workspace scratch store.
#
# The online smoke test cannot stand in for this. Batch runs a different entry
# script (batch_score.py, not score.py), on a different runtime (ParallelRunStep
# on the training cluster, not azmlinfsrv), reading files rather than a JSON
# body, under a different identity, and writing through a different storage
# path. Every batch defect this project hit -- the Table and Queue data-plane
# roles, the dropped `settings` block, the int64 rejection, the discarded
# environment variables, the immutable-container write -- was invisible to the
# online path and to every unit test, and only an invocation surfaced any of it.
#
# Three assertions carry the weight:
#
#   * Exactly one fixture row carries an out-of-corpus vendor and its
#     `unseen_categories` must come back non-empty. A model that silently
#     encodes an unknown vendor as the baseline answers with a confident wrong
#     number that looks exactly like a right one.
#   * Every row must name the model version that produced it. A batch deployment
#     cannot carry environment variables, so the version reaches the container
#     through the code snapshot; if that breaks, the column reads "unknown".
#   * The score object must exist in the scores container afterwards. That is
#     the whole point of the publish step, and it is the part that fails
#     silently -- the scoring job succeeds either way.
set -euo pipefail

env_name="${1:?usage: batch_smoke_test.sh <dev|prod>}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
storage_account="${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"
expected_version="${FIRMAWARE_MODEL_VERSION:-}"
fixture="${FIRMAWARE_SMOKE_FIXTURE:-deploy/azure/fixtures/upcoming_smoke.csv}"
scores_container="${FIRMAWARE_SCORES_CONTAINER:-scores}"

# Referenced so shellcheck sees the same required-variable contract the rest of
# the deploy scripts declare; run_batch_scoring.sh reads them from the
# environment it inherits.
export AZURE_ML_WORKSPACE="${workspace}"
export AZURE_RESOURCE_GROUP="${resource_group}"
export AZURE_STORAGE_ACCOUNT="${storage_account}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

fail() { echo "BATCH SMOKE FAIL: $1" >&2; exit 1; }

count_scores() {
  # Only the objects write_scores produces. The container also holds staging
  # debris from the abandoned attempt to point `--output-path` at it, and that
  # cannot be deleted -- the immutability policy applies to mistakes too.
  az storage fs file list \
    --account-name "${storage_account}" \
    --file-system "${scores_container}" \
    --auth-mode login \
    --query "length([?starts_with(name, 'scores_')])" -o tsv
}

# Everything before the assertions is the real operator path, not a test-only
# one. A smoke test that exercises a different code path than production proves
# only that the test works.
before="$(count_scores)"

output="$(bash deploy/azure/run_batch_scoring.sh "${env_name}" "${fixture}")"
echo "${output}"

scoring_job="$(sed -n 's/^scoring_job=//p' <<<"${output}" | tail -1)"
[[ -n "${scoring_job}" ]] || fail "run_batch_scoring.sh reported no scoring job"

after="$(count_scores)"

# The publish step names the object after the scoring job, so this locates the
# run's own evidence rather than trusting that the newest object is it.
published="$(az storage fs file list \
  --account-name "${storage_account}" \
  --file-system "${scores_container}" \
  --auth-mode login \
  --query "[?starts_with(name, 'scores_') && contains(name, '${scoring_job}')].name" -o tsv)"

[[ "${after}" -gt "${before}" ]] \
  || fail "scores container still holds ${after} objects; nothing was published"
[[ -n "${published}" ]] \
  || fail "no score object names scoring job ${scoring_job}; the publish step wrote elsewhere"

echo "[batch-smoke] published object: ${published}"

scores_file="$(mktemp)"
trap 'rm -f "${scores_file}"' EXIT

# Read back over the blob endpoint, not `az storage fs file download`. The DFS
# Get Properties on an object written by create/append/flush reports no
# contentLength, so the CLI's range request comes back OutOfRangeInput -- "the
# specified resource name length is not within the permissible limits", which
# names the wrong thing entirely. The blob API reports the size correctly, and
# these objects are block blobs regardless of which endpoint addresses them.
az storage blob download \
  --account-name "${storage_account}" \
  --container-name "${scores_container}" \
  --name "${published}" \
  --file "${scores_file}" \
  --auth-mode login \
  --overwrite \
  --no-progress \
  --output none \
  || fail "could not read back ${published}"

python3 - "${scores_file}" "${expected_version}" <<'PY'
import csv
import sys

with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    records = list(csv.DictReader(handle))
expected_version = sys.argv[2]

# The published object is a real CSV with a header, unlike the staged file the
# driver writes; write_scores produced it, so the header is the contract.
required = {
    "deployment_id",
    "risk_probability",
    "risk_prediction",
    "risk_band",
    "unseen_categories",
    "model_run",
    "model_version",
    "threshold",
    "scored_at",
}
if not records:
    raise SystemExit("BATCH SMOKE FAIL: published score object has no rows")
missing = required - set(records[0])
if missing:
    raise SystemExit(f"BATCH SMOKE FAIL: published object missing {sorted(missing)}")
if len(records) != 5:
    raise SystemExit(f"BATCH SMOKE FAIL: expected 5 scored rows, got {len(records)}")

for index, record in enumerate(records):
    if record["risk_prediction"] not in {"GO", "NO_GO"}:
        raise SystemExit(
            f"BATCH SMOKE FAIL: row {index} bad decision {record['risk_prediction']!r}"
        )
    probability = float(record["risk_probability"])
    if not 0.0 <= probability <= 1.0:
        raise SystemExit(f"BATCH SMOKE FAIL: row {index} probability {probability}")

unseen = [r for r in records if r["unseen_categories"] not in ("{}", "")]
if len(unseen) != 1:
    raise SystemExit(
        "BATCH SMOKE FAIL: expected exactly 1 row with unseen categories, got "
        f"{len(unseen)}; the out-of-corpus vendor is being silently encoded"
    )

versions = {r["model_version"] for r in records}
if len(versions) != 1:
    raise SystemExit(f"BATCH SMOKE FAIL: mixed model versions in one run: {versions}")
served = versions.pop()
if served == "unknown":
    raise SystemExit(
        "BATCH SMOKE FAIL: model_version is 'unknown'; the code-snapshot sidecar "
        "written by deploy_batch.sh did not reach the scoring container"
    )
if expected_version and served != expected_version:
    raise SystemExit(
        f"BATCH SMOKE FAIL: scored with model version {served}, expected {expected_version}"
    )

scored_at = {r["scored_at"] for r in records}
if len(scored_at) != 1:
    raise SystemExit(f"BATCH SMOKE FAIL: one run reported several scored_at: {scored_at}")

print(f"[batch-smoke] 5/5 scored, model_version={served}, scored_at={scored_at.pop()}")
print(f"[batch-smoke] unseen categories surfaced on: {unseen[0]['deployment_id']}")
PY

# The container's guarantee is the reason this pipeline writes here at all, so
# it is asserted rather than assumed: a second write to the same object must be
# refused by the platform.
if az storage blob upload \
  --account-name "${storage_account}" \
  --container-name "${scores_container}" \
  --name "${published}" \
  --file "${scores_file}" \
  --auth-mode login \
  --overwrite \
  --output none 2>/dev/null; then
  fail "published score object ${published} could be overwritten; it is not immutable"
fi
echo "[batch-smoke] ${published} is immutable: overwrite was refused"

echo "[batch-smoke] all assertions passed for ${env_name}"
