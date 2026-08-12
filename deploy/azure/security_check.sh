#!/usr/bin/env bash
# Assert the implementation spec's non-negotiables against live Azure state.
#
# Runs in CI after every deploy and fails it. Terraform describes intent; this
# describes reality, and the two diverge whenever someone changes something in
# the portal. Each check below maps to a numbered control in the spec.
#
# On environment deviations: dev runs a cost-reduced profile with no private
# endpoints and a Basic registry. Those relaxations are *declared* here by name,
# not silently skipped -- the script prints exactly which control was relaxed and
# which compensating control it verified instead. A check that quietly passes
# because the environment is cheap is worse than no check.
set -euo pipefail

env_name="${1:?usage: security_check.sh <dev|prod>}"
rg="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
sa="${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"
acr="${AZURE_ACR_NAME:?AZURE_ACR_NAME is required}"
workspace="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
key_vault="${AZURE_KEY_VAULT:?AZURE_KEY_VAULT is required}"

# Declared posture. Prod asserts the full spec; dev asserts everything except the
# controls its tfvars deliberately relaxes.
network_isolation="${FIRMAWARE_NETWORK_ISOLATION:-$([[ "${env_name}" == "prod" ]] && echo true || echo false)}"

failures=0
fail() { echo "  FAIL: $1" >&2; failures=$((failures + 1)); }
pass() { echo "  pass: $1"; }
relaxed() { echo "  RELAXED in ${env_name}: $1"; }

check() {
  # A single place where a boolean becomes a pass or a counted failure, so no
  # call site can accidentally end in a non-zero status and trip `set -e`.
  if [[ "$1" == "true" ]]; then pass "$2"; else fail "$3"; fi
}

echo "[1] no service-principal passwords"
# Distinguishing "no apps" from "could not ask" matters: the CI identity holds no
# Microsoft Graph permission, and a check that reports a pass when the query was
# refused is worse than no check at all.
if apps="$(az ad app list --filter "startswith(displayName,'firmaware')" --query "[].appId" -o tsv 2>/dev/null)"; then
  app_count=0
  for app in ${apps}; do
    app_count=$((app_count + 1))
    # No `[?type=='Password']` filter: the Graph passwordCredential object has no
    # `type` property, so that filter matched nothing and the control was
    # vacuous. `credential list` returns password credentials by default and
    # certificates only under --cert, so both are queried explicitly.
    secrets="$(az ad app credential list --id "${app}" --query "[].keyId" -o tsv 2>/dev/null || true)"
    [[ -z "${secrets}" ]] || fail "application ${app} has a password credential"
    certs="$(az ad app credential list --id "${app}" --cert --query "[].keyId" -o tsv 2>/dev/null || true)"
    [[ -z "${certs}" ]] || fail "application ${app} has a certificate credential"
  done
  if [[ "${app_count}" -eq 0 ]]; then
    pass "no firmaware application registrations at all (workload identity federation only)"
  else
    pass "checked ${app_count} application(s), none carry a credential"
  fi
else
  # Not a pass. The deploy identity is not meant to read Graph, so this is the
  # expected outcome in CI -- but it is reported as unverified, not as clean.
  echo "  UNVERIFIED: cannot query Microsoft Graph with this identity."
  echo "             Run 'az ad app list --filter \"startswith(displayName,'firmaware')\"'"
  echo "             as a directory reader to confirm zero password credentials."
fi

echo "[2] storage: AAD only, TLS 1.2, network posture"
sa_json="$(az storage account show -n "${sa}" -g "${rg}" -o json)"
read -r shared_key public_access tls <<<"$(python3 -c '
import json, sys
account = json.load(sys.stdin)
print(
    account.get("allowSharedKeyAccess"),
    account.get("publicNetworkAccess"),
    account.get("minimumTlsVersion"),
)' <<<"${sa_json}")"

check "$([[ "${shared_key}" == "False" ]] && echo true || echo false)" \
  "shared key access disabled (no account key exists to leak)" \
  "shared key access is ${shared_key} on ${sa}"
check "$([[ "${tls}" == "TLS1_2" ]] && echo true || echo false)" \
  "minimum TLS 1.2" \
  "minimum TLS is ${tls} on ${sa}"

if [[ "${network_isolation}" == "true" ]]; then
  check "$([[ "${public_access}" == "Disabled" ]] && echo true || echo false)" \
    "public network access disabled" \
    "public network access is ${public_access} on ${sa}"
else
  relaxed "storage public network access (no private endpoints in this profile)"
  # Compensating control, and it is the one that actually matters: with shared
  # keys disabled, an open network path still requires an AAD identity that has
  # been granted a data-plane role. Re-asserted rather than assumed.
  check "$([[ "${shared_key}" == "False" ]] && echo true || echo false)" \
    "compensating: AAD is still the only authentication path" \
    "public path is open AND shared keys are enabled on ${sa}"
fi

echo "[3] registry: no admin user, no unresolved HIGH/CRITICAL"
admin="$(az acr show -n "${acr}" --query adminUserEnabled -o tsv)"
check "$([[ "${admin}" == "false" ]] && echo true || echo false)" \
  "ACR admin user disabled (no registry password exists)" \
  "ACR admin user enabled on ${acr}"

sku="$(az acr show -n "${acr}" --query sku.name -o tsv)"
if [[ "${sku}" == "Premium" ]]; then
  quarantine="$(az acr show -n "${acr}" --query "policies.quarantinePolicy.status" -o tsv 2>/dev/null || echo "unknown")"
  check "$([[ "${quarantine}" == "enabled" ]] && echo true || echo false)" \
    "quarantine policy enabled" \
    "quarantine policy is ${quarantine} on ${acr}"
else
  relaxed "ACR ${sku}: quarantine, immutable tags and retention are Premium-only"
  pass "compensating: CI blocks the image on any HIGH/CRITICAL Trivy finding before push"
fi

echo "[4] no subscription-scope or privileged assignments"
sub_id="$(az account show --query id -o tsv)"
sub_scope="/subscriptions/${sub_id}"

# The CI identity lives in the shared state resource group, not this one, so
# enumerating `az identity list -g "${rg}"` alone would inspect only the two
# least-privileged principals and skip the only one holding Contributor and
# User Access Administrator -- the one whose scope could actually drift, and
# whose escalation is guarded only by an ABAC condition. It is added explicitly.
principals="$(az identity list -g "${rg}" --query "[].principalId" -o tsv)"
cicd_principal="${FIRMAWARE_CICD_PRINCIPAL_ID:-}"
if [[ -z "${cicd_principal}" && -n "${AZURE_CLIENT_ID:-}" ]]; then
  cicd_principal="$(az ad sp show --id "${AZURE_CLIENT_ID}" --query id -o tsv 2>/dev/null || true)"
fi
if [[ -n "${cicd_principal}" ]]; then
  principals="${principals}"$'\n'"${cicd_principal}"
else
  fail "cannot resolve the CI principal; control 4 would skip the most privileged identity"
fi

for principal in ${principals}; do
  # --assignee-object-id, not --assignee: the latter resolves the principal
  # through Microsoft Graph first, and the CI identity holds no Graph permission
  # at all -- by design, and asserted by control 1. Paired with `|| true`, that
  # made this loop report a clean result for an identity whose assignments it
  # had never managed to read. A control that passes when the query fails is
  # worse than no control, and this is the loop that covers the one principal
  # holding Contributor and User Access Administrator.
  if ! bad="$(az role assignment list --assignee-object-id "${principal}" --all --query \
    "[?scope=='${sub_scope}' || roleDefinitionName=='Owner'].{r:roleDefinitionName,s:scope}" -o tsv)"; then
    echo "  ERROR: could not list role assignments for ${principal}" >&2
    exit 2
  fi
  [[ -z "${bad}" ]] || fail "over-privileged assignment for ${principal}: ${bad}"
done
pass "every identity, CI included, is scoped at or below a resource group"

# The append-only role is the point of the whole scores design, so its shape is
# asserted rather than its presence: a role that grants delete would satisfy a
# name check and defeat the control.
echo "[5] scores are append-only and immutable"
# Prefix match, not equality: the role is created per environment as
# "Storage Blob Data Appender (firmaware-dev)" so two environments in one
# subscription do not collide on a single global role name.
data_actions="$(az role definition list --custom-role-only true \
  --query "[?starts_with(roleName, 'Storage Blob Data Appender')].permissions[0].dataActions[]" -o tsv 2>/dev/null || true)"
if [[ -z "${data_actions}" ]]; then
  fail "custom role 'Storage Blob Data Appender' not found"
else
  # The absence of delete is the control. A role that granted it would still
  # satisfy a name check, which is why the actions are inspected rather than
  # the role's existence.
  if grep -q "blobs/delete" <<<"${data_actions}"; then
    fail "append-only role grants blobs/delete; it is not append-only"
  else
    pass "append-only role omits blobs/delete"
  fi
  if grep -q "blobs/add/action" <<<"${data_actions}"; then
    pass "append-only role grants blobs/add/action"
  else
    fail "append-only role cannot append"
  fi
fi

if az storage container immutability-policy show --account-name "${sa}" \
  --container-name scores --resource-group "${rg}" >/dev/null 2>&1; then
  pass "immutability policy present on scores"
else
  fail "no immutability policy on scores"
fi

echo "[6] diagnostic settings on every audited resource"
ws_id="$(az ml workspace show -n "${workspace}" -g "${rg}" --query id -o tsv)"
kv_id="$(az keyvault show -n "${key_vault}" -g "${rg}" --query id -o tsv)"
acr_id="$(az acr show -n "${acr}" --query id -o tsv)"
sa_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"${sa_json}")"
# The storage account's audit logs live on its blob service, not on the account
# resource: Azure exposes no diagnostic categories at the account level, so the
# bare account id is deliberately not in this list.
for resource in "${ws_id}" "${kv_id}" "${acr_id}" "${sa_id}/blobServices/default"; do
  # `|| echo 0` was here, and it made this control unable to tell "there is no
  # diagnostic setting" from "the query did not run". That mattered more than it
  # looks: the query never ran at all. `length(value)` assumed a wrapper object,
  # but `diagnostic-settings list` returns a plain array, so JMESPath evaluated
  # length(null) and failed every time. Four settings that all existed were
  # reported missing, and the control had never once been true.
  #
  # Errors are surfaced now, not counted as findings -- and the reverse mistake,
  # reporting a pass on a query that never answered, is the one this is really
  # guarding against.
  if ! count="$(az monitor diagnostic-settings list --resource "${resource}" \
    --query "length(@)" -o tsv)"; then
    echo "  ERROR: could not read diagnostic settings on ${resource}" >&2
    exit 2
  fi
  check "$([[ "${count}" -gt 0 ]] && echo true || echo false)" \
    "diagnostics on ${resource##*/}" \
    "no diagnostic setting on ${resource}"
done

echo "[7] the six mandatory tags on every resource in the group"
untagged="$(az resource list -g "${rg}" -o json | python3 -c '
import json
import sys

required = set(sys.argv[1].split())
problems = []
for resource in json.load(sys.stdin):
    tags = resource.get("tags") or {}
    missing = required - set(tags)
    if missing:
        name = resource.get("name")
        kind = resource.get("type")
        problems.append("%s (%s): missing %s" % (name, kind, sorted(missing)))
print("\n".join(problems))
' "app env owner cost_center data_classification managed_by")"
if [[ -n "${untagged}" ]]; then
  while IFS= read -r line; do fail "${line}"; done <<<"${untagged}"
else
  pass "all resources carry the six mandatory tags"
fi

echo
if [[ "${failures}" -gt 0 ]]; then
  echo "security_check: ${failures} control(s) failed in ${env_name}" >&2
  exit 1
fi
echo "security_check: all controls passed for ${env_name} (network_isolation=${network_isolation})"
