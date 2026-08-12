#!/usr/bin/env bash
# Print the resource group an environment's resources live in.
#
# Terraform is the only place that decides this, and it decides it in
# envs/<env>.tfvars. Repeating the answer in a workflow's `env:` block is how the
# two drift: dev overrides the conventional rg-<prefix> name with a single shared
# group called "firmAware", the workflow kept saying rg-firmaware-dev, and every
# `az` call in the deploy then queried a group that does not exist. Nothing
# errors -- `az acr list` on a missing group returns an empty list -- so the run
# fails several steps later with an empty registry name and no hint why.
#
# So this reads the tfvars instead of restating it, and falls back to the same
# rg-firmaware-<env> default that main.tf's locals use when the variable is null.
set -euo pipefail

env_name="${1:?usage: resource_group.sh <dev|prod>}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tfvars="${repo_root}/infra/azure/envs/${env_name}.tfvars"

if [[ ! -f "${tfvars}" ]]; then
  echo "missing ${tfvars}" >&2
  exit 1
fi

# Anchored to the line start so a commented-out example cannot be read as the
# real setting.
resource_group="$(sed -n \
  's/^resource_group_name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
  "${tfvars}" | head -1)"

# Mirrors `local.resource_group_name` in infra/azure/main.tf, which is
# coalesce(var.resource_group_name, "rg-${local.name_prefix}") with
# name_prefix = "firmaware-${var.env}".
if [[ -z "${resource_group}" ]]; then
  resource_group="rg-firmaware-${env_name}"
fi

echo "${resource_group}"
