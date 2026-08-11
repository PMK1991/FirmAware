#!/usr/bin/env bash
# One-time-per-subscription bootstrap: Terraform state, and the federated
# identity CI uses to reach it.
#
# This is the only script that is allowed to create anything outside Terraform,
# for the obvious reason that Terraform cannot store its own state before its
# state store exists. Everything else in this repository is applied, not run.
#
# Idempotent throughout: every create is guarded by a show, so re-running after a
# partial failure converges rather than erroring. A second run must be a no-op --
# that is acceptance criterion 1's "no portal clicks" in practice, because if
# this needed manual repair it would not be reproducible.
set -euo pipefail

# Git Bash and MSYS on Windows rewrite arguments that look like Unix paths into
# Windows paths before the process ever sees them. An ARM scope such as
# /subscriptions/<id>/resourceGroups/... becomes C:/Program Files/Git/subscriptions/...,
# and the request then arrives at Azure with no subscription at all, failing with
# a MissingSubscription error that names neither the scope nor the cause. This
# script is meant to be run by a human, and plenty of humans are on Windows.
# Both variables are inert on Linux and macOS.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

env_name="${1:?usage: bootstrap.sh <dev|prod>}"
location="${AZURE_LOCATION:-eastus}"
subscription_id="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
suffix="${AZURE_STATE_SUFFIX:-fa7k}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

# The GitHub environment name is not always the Terraform env name: prod deploys
# from the `production` environment. This is what lands in the OIDC subject
# claim, so it must match the `environment:` declared by the deploy workflow.
if [[ "${env_name}" == "prod" ]]; then
  github_environment="${GITHUB_ENVIRONMENT:-production}"
else
  github_environment="${GITHUB_ENVIRONMENT:-dev}"
fi

# This script is the single source of truth for the federated subject. It is
# deliberately not in tfvars: Terraform does not create the credential, and
# duplicating the values would let them drift into a subject that matches
# nothing, which fails as an unauthorized token exchange rather than as a plan.
github_owner="${GITHUB_OWNER:-PMK1991}"
github_repo="${GITHUB_REPO:-FirmAware}"

state_rg="rg-firmaware-tfstate"
state_sa="stfirmawaretf${suffix}"
state_container="tfstate-${env_name}"
identity_name="id-firmaware-bootstrap-${env_name}"
identity_rg="${state_rg}"

az account set --subscription "${subscription_id}"

echo "[bootstrap] registering resource providers"
# Registration is asynchronous and silently slow; an unregistered provider fails
# the first apply with an error that does not name the provider clearly.
for provider in Microsoft.MachineLearningServices Microsoft.ContainerRegistry \
  Microsoft.Storage Microsoft.KeyVault Microsoft.OperationalInsights \
  Microsoft.Insights Microsoft.PolicyInsights Microsoft.Network \
  Microsoft.ManagedIdentity Microsoft.Authorization; do
  state="$(az provider show --namespace "${provider}" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
  if [[ "${state}" != "Registered" ]]; then
    echo "  registering ${provider} (currently ${state})"
    az provider register --namespace "${provider}" --wait
  fi
done

echo "[bootstrap] state resource group"
az group create --name "${state_rg}" --location "${location}" \
  --tags app=firmaware env=shared owner=platform cost_center=firmaware-rnd \
  data_classification=internal managed_by=bootstrap \
  --output none

echo "[bootstrap] state storage account"
if ! az storage account show -n "${state_sa}" -g "${state_rg}" >/dev/null 2>&1; then
  az storage account create \
    --name "${state_sa}" \
    --resource-group "${state_rg}" \
    --location "${location}" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    --allow-shared-key-access false \
    --https-only true \
    --tags app=firmaware env=shared owner=platform cost_center=firmaware-rnd \
    data_classification=internal managed_by=bootstrap \
    --output none
fi

# Versioning is the state rollback mechanism named in the architecture's infra
# rollback class: a corrupted state file is restored from a prior blob version
# rather than reconstructed by hand.
az storage account blob-service-properties update \
  --account-name "${state_sa}" \
  --resource-group "${state_rg}" \
  --enable-versioning true \
  --enable-delete-retention true \
  --delete-retention-days 30 \
  --output none

echo "[bootstrap] state container"
# --auth-mode login because shared keys are disabled on this account: there is no
# key to pass, which is the point.
az storage container create \
  --name "${state_container}" \
  --account-name "${state_sa}" \
  --auth-mode login \
  --output none

echo "[bootstrap] bootstrap identity"
# This identity is what GitHub Actions becomes. It is created here rather than by
# Terraform because Terraform cannot create the credential that authenticates the
# apply which creates it.
az identity create --name "${identity_name}" --resource-group "${identity_rg}" \
  --location "${location}" \
  --tags app=firmaware env="${env_name}" owner=platform cost_center=firmaware-rnd \
  data_classification=internal managed_by=bootstrap \
  --output none

identity_client_id="$(az identity show --name "${identity_name}" \
  --resource-group "${identity_rg}" --query clientId -o tsv)"
identity_principal_id="$(az identity show --name "${identity_name}" \
  --resource-group "${identity_rg}" --query principalId -o tsv)"
identity_id="$(az identity show --name "${identity_name}" \
  --resource-group "${identity_rg}" --query id -o tsv)"

# One credential, not two. Both deploy workflows declare a GitHub environment,
# and GitHub puts that environment into the OIDC subject when they do -- so a
# branch-subject credential would never match, and for prod it would be an
# escalation path around the required-reviewer gate. The subject is scoped to the
# environment being bootstrapped, so bootstrapping dev grants nothing in prod.
#
# Created here rather than in Terraform on purpose: an identity that can write
# its own federated credentials can add a subject for any branch or environment,
# which defeats the gate the credential exists to enforce. This runs as a human,
# so the trust decision is made by someone the directory already trusts.
add_federation() {
  local name="$1" subject="$2"
  if az identity federated-credential show --name "${name}" \
    --identity-name "${identity_name}" --resource-group "${identity_rg}" >/dev/null 2>&1; then
    echo "  federation ${name} exists"
    return
  fi
  az identity federated-credential create \
    --name "${name}" \
    --identity-name "${identity_name}" \
    --resource-group "${identity_rg}" \
    --issuer "https://token.actions.githubusercontent.com" \
    --subject "${subject}" \
    --audiences "api://AzureADTokenExchange" \
    --output none
}

add_federation "github-env-${github_environment}" \
  "repo:${github_owner}/${github_repo}:environment:${github_environment}"

echo "[bootstrap] granting the bootstrap identity access to state only"
# Deliberately narrow: this identity can read and write Terraform state, and read
# its own identity resource. The broader Contributor grant it needs to apply is
# created by Terraform itself, at resource-group scope, in the identity module.

# A freshly created managed identity's service principal takes time to replicate
# through AAD, and a role assignment attempted before it lands fails with
# PrincipalNotFound. That is transient, so it is retried -- but it is NOT the
# same as "already present", and conflating the two is how an environment ends
# up looking bootstrapped while holding no permissions at all. The failure then
# surfaces much later, as an AuthorizationFailed in CI that points at nothing.
#
# So: retry the transient case, accept only a genuine RoleAssignmentExists as a
# no-op, and verify the assignment is really there before returning.
ensure_role_assignment() {
  local principal="$1" principal_type="$2" role="$3" scope="$4" attempt output
  for attempt in 1 2 3 4 5 6; do
    if output="$(az role assignment create \
      --assignee-object-id "${principal}" \
      --assignee-principal-type "${principal_type}" \
      --role "${role}" \
      --scope "${scope}" \
      --output none 2>&1)"; then
      echo "  granted ${role}"
      return 0
    fi
    if grep -qi "RoleAssignmentExists" <<<"${output}"; then
      echo "  ${role} already present"
      return 0
    fi
    echo "  ${role} attempt ${attempt} failed, retrying in $((attempt * 10))s"
    sleep $((attempt * 10))
  done

  echo "ERROR: could not grant ${role} on ${scope}" >&2
  echo "${output}" >&2
  return 1
}

state_container_id="$(az storage account show -n "${state_sa}" -g "${state_rg}" --query id -o tsv)/blobServices/default/containers/${state_container}"
ensure_role_assignment "${identity_principal_id}" ServicePrincipal \
  "Storage Blob Data Contributor" "${state_container_id}"

# Terraform reads this identity with a data source, which is a control-plane GET
# on the identity resource. Without this the very first CI `terraform plan` fails
# with AuthorizationFailed before it evaluates anything else -- the identity
# would not be able to read itself. Reader, not Managed Identity Contributor:
# read is all the data source needs, and write would let the pipeline add
# federated credentials to its own identity and authorise new subjects.
ensure_role_assignment "${identity_principal_id}" ServicePrincipal \
  "Reader" "${identity_id}"

# The operator running this script has to run the first apply too, and that apply
# reads and writes state like any other. Subscription Owner does NOT grant it:
# blob data actions live in separate data-plane roles, so an Owner who skips this
# meets a bare 403 AuthorizationPermissionMismatch on `terraform init` with no
# indication that a role is missing. Granting it here keeps the documented path
# working rather than leaving it as folklore.
operator_id="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
if [[ -n "${operator_id}" ]]; then
  echo "[bootstrap] granting the operator state access for the first apply"
  ensure_role_assignment "${operator_id}" User \
    "Storage Blob Data Contributor" "${state_container_id}"
else
  echo "[bootstrap] WARNING: could not resolve the signed-in user."
  echo "  If you are a human running the first apply, grant yourself"
  echo "  'Storage Blob Data Contributor' on the state container, or"
  echo "  'terraform init' will fail with 403 AuthorizationPermissionMismatch."
fi

# Verify rather than assume. Everything above is idempotent and therefore easy to
# re-run, which is exactly what makes a silent partial failure dangerous: the
# second run prints the same reassuring output as the first.
echo "[bootstrap] verifying grants"
held="$(az role assignment list --assignee "${identity_principal_id}" --all \
  --query "[].roleDefinitionName" -o tsv)"
for required in "Storage Blob Data Contributor" "Reader"; do
  if ! grep -qxF "${required}" <<<"${held}"; then
    echo "ERROR: ${required} is missing after bootstrap" >&2
    exit 1
  fi
  echo "  confirmed ${required}"
done

backend_file="infra/azure/envs/${env_name}.backend.hcl"
cat > "${backend_file}" <<EOF
# Generated by infra/azure/bootstrap.sh. SAFE TO COMMIT, AND IT MUST BE: the
# deploy workflows run 'terraform init -backend-config=envs/${env_name}.backend.hcl',
# so a git-ignored file would leave CI unable to find its own state. Nothing
# here is a secret -- these are resource names, and authentication is the OIDC
# token the workflow already holds.
#
#   terraform init -backend-config=envs/${env_name}.backend.hcl
resource_group_name  = "${state_rg}"
storage_account_name = "${state_sa}"
container_name       = "${state_container}"
key                  = "firmaware-${env_name}.tfstate"
EOF

echo
echo "[bootstrap] complete for ${env_name}"
echo "  backend config: ${backend_file}"
echo "  COMMIT THIS FILE -- the deploy workflow reads it to locate remote state:"
echo "    git add ${backend_file} && git commit -m 'chore: azure ${env_name} backend config'"
echo
echo "Create a GitHub environment named '${github_environment}', and set these as"
echo "that ENVIRONMENT's variables -- not repository variables:"
echo "  AZURE_CLIENT_ID       = ${identity_client_id}"
echo "  AZURE_TENANT_ID       = $(az account show --query tenantId -o tsv)"
echo "  AZURE_SUBSCRIPTION_ID = ${subscription_id}"
echo
echo "Environment-scoped, because bootstrap creates a SEPARATE identity per"
echo "environment (id-firmaware-bootstrap-dev vs -prod). Setting AZURE_CLIENT_ID"
echo "at repository scope means bootstrapping the second environment overwrites"
echo "the first, and the OIDC exchange then fails for whichever one lost -- no"
echo "federated credential matches the subject it presents."
echo
echo "None of the three is a secret. Marking them secret only makes logs unreadable."
echo
echo "This identity is the only one. Terraform reads it as a data source and"
echo "grants it the rest of its access; there is no second 'cicd' identity."
echo
echo "Next, and it must be a human the first time -- CI can authenticate now, but"
echo "it holds no rights beyond state until this apply grants them:"
echo "  cd infra/azure"
echo "  terraform init -backend-config=envs/${env_name}.backend.hcl"
echo "  terraform apply -var-file=envs/${env_name}.tfvars"
