#!/usr/bin/env bash
# Prove a revision of the page is safe to promote, before it takes any traffic.
#
# Everything here runs against the revision's OWN hostname, so the live revision
# keeps serving throughout and a failure costs nothing but a deploy.
#
# What this checks, and one thing it deliberately does not.
#
# It checks that the revision is up, that it is the image that was built, that it
# is configured with the ADLS paths this deployment created, that it holds no
# secrets, and that the identity behind it can read those paths and can write
# nothing at all.
#
# It does NOT assert on the rendered sidebar caption, which the hosting spec
# asks for. Streamlit serves a bootstrap page over HTTP and then renders the
# script over a websocket, so the caption naming the backing store is never in
# the HTML any HTTP client receives. A grep for it would not be a weak check --
# it would be a check that can only ever fail, or, if written the usual way
# round, one that silently never matches. The configuration and the RBAC
# assertions below are what stand in for it: together they say the page is
# pointed at the lake and is able to read it.
set -euo pipefail

env_name="${1:?usage: smoke_test_app.sh <dev|prod> <revision>}"
revision="${2:?revision is required: smoke test the revision, not the endpoint}"

resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
expected_digest="${FIRMAWARE_IMAGE_DIGEST:?FIRMAWARE_IMAGE_DIGEST is required}"
expected_scores_uri="${FIRMAWARE_SCORES_URI:?FIRMAWARE_SCORES_URI is required}"
expected_client_id="${FIRMAWARE_APP_CLIENT_ID:?FIRMAWARE_APP_CLIENT_ID is required}"
app_principal_id="${FIRMAWARE_APP_PRINCIPAL_ID:?FIRMAWARE_APP_PRINCIPAL_ID is required}"
scores_scope="${FIRMAWARE_SCORES_CONTAINER_ID:-}"

container_app="ca-firmaware-${env_name}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

az_app() { az containerapp "$@" --name "${container_app}" --resource-group "${resource_group}"; }

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  pass: $*"; }

fqdn="$(az_app revision show --revision "${revision}" --query "properties.fqdn" -o tsv)"
[[ -n "${fqdn}" && "${fqdn}" != "None" ]] || fail "revision ${revision} has no FQDN; is ingress external?"

echo "[smoke-app] ${revision}"
echo "[smoke-app] https://${fqdn}"

# --- [1] the revision serves --------------------------------------------------
# Generous, because this is a cold start by construction: the revision holds no
# traffic and dev runs at min_replicas 0, so the first request has to pull the
# image and import pandas, plotly and the ADLS client before Streamlit answers.
# Requesting the revision's own hostname is what activates it.

echo "[1] the revision answers on its own hostname"

status=""
for attempt in $(seq 1 40); do
  status="$(curl --silent --show-error --location --max-time 30 \
    --output /dev/null --write-out '%{http_code}' \
    "https://${fqdn}/_stcore/health" || echo "000")"
  [[ "${status}" == "200" ]] && break
  echo "  waiting for the revision to answer (${attempt}/40, last status ${status})"
  sleep 15
done
[[ "${status}" == "200" ]] || fail "/_stcore/health returned ${status} after ~10 minutes"
pass "/_stcore/health returns 200"

# --- [2] the page is reachable, and anonymously ------------------------------
# The hosting spec makes Entra ID authentication a non-negotiable and expects a
# 302 to a login page here. That was overridden deliberately: this page is meant
# to be shareable by link, and the decision is recorded in infra/azure/README.md
# under "A public page".
#
# So the assertion is inverted rather than dropped. If someone later enables
# authentication without updating that record, this check fails and says so --
# which is the correct outcome either way, because a deployment whose access
# model has silently changed is a deployment nobody can reason about.

echo "[2] the page is reachable without credentials"

root_status="$(curl --silent --show-error --location --max-time 60 \
  --output /dev/null --write-out '%{http_code}' "https://${fqdn}/" || echo "000")"
case "${root_status}" in
  200) pass "GET / returns 200 to an unauthenticated caller, as intended" ;;
  302|401|403)
    fail "GET / returned ${root_status}: something is requiring authentication. \
That may well be an improvement, but it contradicts the recorded decision -- \
update 'A public page' in infra/azure/README.md and this check together." ;;
  *) fail "GET / returned ${root_status}" ;;
esac

# --- [3] HTTPS only -----------------------------------------------------------

echo "[3] there is no plaintext listener"

http_status="$(curl --silent --show-error --max-time 30 \
  --output /dev/null --write-out '%{http_code}' "http://${fqdn}/" || echo "000")"
case "${http_status}" in
  301|302|307|308) pass "plain HTTP is redirected (${http_status}), not served" ;;
  000) pass "plain HTTP is refused outright" ;;
  *) fail "http:// returned ${http_status}: allow_insecure_connections should make this impossible" ;;
esac

# --- [4] it is the image that was built --------------------------------------

echo "[4] the revision is pinned to the image that was scanned"

actual_image="$(az_app revision show --revision "${revision}" \
  --query "properties.template.containers[0].image" -o tsv)"
[[ "${actual_image}" == "${expected_digest}" ]] \
  || fail "revision runs ${actual_image}, expected ${expected_digest}"
[[ "${actual_image}" =~ @sha256:[0-9a-f]{64}$ ]] \
  || fail "revision is running a tag, not a digest: ${actual_image}"
pass "image is ${actual_image}"

# --- [5] no secrets -----------------------------------------------------------
# `length(@)` rather than `length(value)`, and no `|| echo 0`. The security gate
# learned this the hard way: a query that errors must fail the check, not be
# laundered into a passing zero.

echo "[5] the app holds no secrets"

secret_count="$(az_app show --query "length(properties.configuration.secrets)" -o tsv 2>/dev/null)" \
  || fail "could not read the app's secret list; treating an unreadable control as unverified, not as a pass"
[[ "${secret_count}" == "0" || -z "${secret_count}" ]] \
  || fail "the app declares ${secret_count} secret(s); storage and the registry are both reached with the managed identity, so there is nothing a secret should be holding"
pass "properties.configuration.secrets is empty"

# --- [6] configured against this deployment's own paths -----------------------

echo "[6] the page is pointed at the lake, not at the bundled sample"

env_json="$(az_app revision show --revision "${revision}" \
  --query "properties.template.containers[0].env" -o json)"

python3 -c '
import json, sys

env = {item["name"]: item.get("value", "") for item in json.loads(sys.argv[1] or "[]")}
expected_scores, expected_client = sys.argv[2], sys.argv[3]

problems = []

scores = env.get("FIRMAWARE_SCORES_URI", "")
if scores != expected_scores:
    problems.append(f"FIRMAWARE_SCORES_URI is {scores!r}, expected {expected_scores!r}")
if not scores.startswith("abfss://"):
    problems.append(
        f"FIRMAWARE_SCORES_URI is {scores!r}: not an ADLS path, so the page would "
        "fall back to the demo fixtures committed in the repo"
    )

# app.py resolves DEMO_DIR relative to itself and falls back to it whenever a
# path does not resolve. That fallback is right for a fresh clone and wrong for a
# deployment, where it would render sample data with no visible sign it is not
# real. Every source is therefore named explicitly.
for name in ("FIRMAWARE_ARTIFACTS_URI", "FIRMAWARE_DATA_URI", "FIRMAWARE_UPCOMING_URI"):
    value = env.get(name, "")
    if not value.startswith("abfss://"):
        problems.append(f"{name} is {value!r}, expected an abfss:// path")

# Without this, DefaultAzureCredential cannot tell which user-assigned identity
# it is meant to be and fails on the first read -- with an error that reads like
# the identity is missing rather than ambiguous.
if env.get("AZURE_CLIENT_ID") != expected_client:
    problems.append(
        f"AZURE_CLIENT_ID is {env.get('AZURE_CLIENT_ID')!r}, expected {expected_client!r}"
    )

if problems:
    raise SystemExit("\n".join(f"FAIL: {problem}" for problem in problems))
' "${env_json}" "${expected_scores_uri}" "${expected_client_id}" || exit 1
pass "every source is an abfss:// path from this deployment"
pass "AZURE_CLIENT_ID names the app identity"

# --- [7] the identity can read, and can write nothing -------------------------
# The compensating control for a page anyone can open. If this drifts, the
# public-access decision stops being defensible, so it is asserted on every
# deploy rather than reviewed occasionally.
#
# --assignee-object-id, not --assignee: the deploy identity cannot read Microsoft
# Graph, and the name-resolving form would fail rather than return nothing.

echo "[7] the app identity is read-only"

assignments="$(az role assignment list \
  --assignee-object-id "${app_principal_id}" \
  --all --query "[].{role:roleDefinitionName, scope:scope}" -o json 2>/dev/null)" \
  || fail "could not list role assignments for the app identity; an unreadable control is unverified, not passed"

python3 -c '
import json, sys

assignments = json.loads(sys.argv[1] or "[]")
roles = sorted(item["role"] for item in assignments)

# Exactly what modules/identity/main.tf grants this principal. Any addition is a
# change to the blast radius of an internet-facing container, so the check is an
# equality rather than a subset test.
expected = sorted(
    ["Storage Blob Data Reader"] * 3 + ["AcrPull"]
)

# Named individually so the failure says which one appeared, rather than just
# that the set differs.
forbidden = {
    "Storage Blob Data Contributor": "would let the page overwrite what it displays",
    "Contributor": "is control-plane write over the whole scope",
    "Owner": "needs no explanation",
    "User Access Administrator": "could re-grant anything to anyone",
    "Key Vault Administrator": "the page has no business in the vault",
    "Key Vault Secrets User": "the page has no business in the vault",
}
for item in assignments:
    role = item["role"]
    if role in forbidden:
        raise SystemExit(f"FAIL: app identity holds {role!r}, which {forbidden[role]}")
    if role.startswith("Storage Blob Data Appender"):
        raise SystemExit(
            "FAIL: app identity holds the append-only scores role. That role is how "
            "the batch pipeline adds evidence; a page that displays evidence must "
            "not be able to add to it."
        )

if roles != expected:
    raise SystemExit(
        f"FAIL: app identity holds {roles}, expected exactly {expected}"
    )
' "${assignments}" || exit 1
pass "exactly four assignments: three Storage Blob Data Reader, one AcrPull"
pass "no write role, no append role, no vault role, no Contributor"

# One more, and it is the assertion the others only imply: read access to the
# scores container specifically. The three readers above could in principle be on
# any three containers.
if [[ -n "${scores_scope}" ]]; then
  scores_role="$(python3 -c '
import json, sys
assignments = json.loads(sys.argv[1] or "[]")
scope = sys.argv[2].lower()
print(next((item["role"] for item in assignments if item["scope"].lower() == scope), ""))
' "${assignments}" "${scores_scope}")"
  [[ "${scores_role}" == "Storage Blob Data Reader" ]] \
    || fail "app identity holds ${scores_role:-nothing} on the scores container, expected Storage Blob Data Reader"
  pass "read, and only read, on the scores container itself"
fi

echo
echo "[smoke-app] revision : ${revision}"
echo "[smoke-app] image    : ${actual_image}"
echo "[smoke-app] scores   : ${expected_scores_uri}"
echo "[smoke-app] url      : https://${fqdn}"
echo "[smoke-app] all checks passed; safe to promote"
