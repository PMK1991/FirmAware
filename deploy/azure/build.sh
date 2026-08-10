#!/usr/bin/env bash
# Build and push the FirmAware image to ACR, and emit its digest.
#
# The digest is the release identifier from here on. Tags are mutable and a tag
# can be moved between the plan and the apply; a digest cannot. Every downstream
# step -- the AML environment, the deployments, the prod promotion check --
# refers to the image by digest only.
#
# Idempotent: if the git SHA has already been pushed, the existing digest is
# returned rather than rebuilt. A rerun of the same commit therefore cannot
# produce a different image, which is what makes "promote what was tested" a
# check rather than a hope.
set -euo pipefail

env_name="${1:-dev}"
registry="${AZURE_ACR_NAME:?AZURE_ACR_NAME is required}"
repository="${FIRMAWARE_IMAGE_REPOSITORY:-firmaware}"
git_sha="${FIRMAWARE_GIT_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

login_server="$(az acr show --name "${registry}" --query loginServer -o tsv)"
image="${login_server}/${repository}"

emit() {
  local digest="$1"
  if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "ACR returned an invalid digest: ${digest}" >&2
    exit 1
  fi
  echo "image_digest=${image}@${digest}"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
      echo "image_digest=${image}@${digest}"
      echo "digest=${digest}"
      echo "login_server=${login_server}"
    } >> "${GITHUB_OUTPUT}"
  fi
}

if digest="$(az acr repository show \
  --name "${registry}" \
  --image "${repository}:${git_sha}" \
  --query digest -o tsv 2>/dev/null)" && [[ -n "${digest}" ]]; then
  emit "${digest}"
  exit 0
fi

# `az acr build` builds inside the registry, so the runner never needs a Docker
# daemon and never holds registry credentials -- the OIDC token it already has is
# enough. It also means the build cannot pick up anything from the runner's local
# image cache.
az acr build \
  --registry "${registry}" \
  --image "${repository}:${git_sha}" \
  --platform linux/amd64 \
  --file Dockerfile \
  --build-arg PIP_EXTRAS=train,azure \
  --target azureml \
  .

digest="$(az acr repository show \
  --name "${registry}" \
  --image "${repository}:${git_sha}" \
  --query digest -o tsv)"
emit "${digest}"
