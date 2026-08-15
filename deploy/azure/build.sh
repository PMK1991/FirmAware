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
#
# That guarantee holds only while the tag actually identifies the content, and
# the tag is a commit SHA while the build context is the *working tree*. On a
# dirty tree the two disagree, and the short-circuit above then returns whatever
# was pushed under that SHA earlier -- silently, and with no relationship to the
# code on disk. This is not hypothetical: it served a day-old image predating
# three fixes, and the resulting failure surfaced two deploys later as a model
# that would not load, which looks nothing like a build problem.
#
# So a dirty tree is refused rather than mislabelled. CI always builds a clean
# checkout, so nothing changes there. Locally, either commit -- which is what
# makes the SHA true -- or set FIRMAWARE_ALLOW_DIRTY=1 to build under a tag
# derived from the content itself, which is honest about not being a commit.
set -euo pipefail

env_name="${1:-dev}"
registry="${AZURE_ACR_NAME:?AZURE_ACR_NAME is required}"
repository="${FIRMAWARE_IMAGE_REPOSITORY:-firmaware}"
git_sha="${FIRMAWARE_GIT_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"

# Which Dockerfile stage, and with which extras. Defaulted to the ML image so
# every existing caller is unchanged; the page passes `app`/`app,azure`.
#
# The two are separate variables rather than one "flavour" because the Dockerfile
# already treats them separately, and pairing them wrongly is a build-time error
# with a clear message (each stage guards its own imports) rather than a silent
# one.
target="${FIRMAWARE_IMAGE_TARGET:-azureml}"
extras="${PIP_EXTRAS:-train,azure}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

# What the chosen stage actually copies. The `app` stage adds the page, its demo
# fixtures and the Streamlit config on top of the common set, and those have to
# be in both lists below or the guarantees break in opposite directions: missing
# from the dirty check, an edited app.py is served as a clean commit's image;
# missing from the content hash, two different pages share one tag.
build_inputs=(Dockerfile pyproject.toml README.md config.yaml src)
if [[ "${target}" == "app" ]]; then
  build_inputs+=(app.py demo .streamlit)
fi

# Hashes exactly what the Dockerfile copies, so the tag changes if and only if
# the image would. Ordered by `sort` because directory iteration order is not
# guaranteed and an unstable hash would defeat the point.
content_tag() {
  local listing
  listing="$(
    {
      printf '%s\n' "${extras}" "${target}"
      find "${build_inputs[@]}" -type f -print0 \
        | sort -z | xargs -0 sha256sum
    } | sha256sum | cut -c1-16
  )"
  printf 'dirty-%s' "${listing}"
}

if [[ -n "${FIRMAWARE_GIT_SHA:-${GITHUB_SHA:-}}" ]]; then
  # An explicit SHA means the caller controls the checkout (CI does), so the
  # working tree is not this script's to judge.
  dirty=""
else
  dirty="$(git status --porcelain --untracked-files=no -- "${build_inputs[@]}")"
fi

if [[ -n "${dirty}" ]]; then
  if [[ "${FIRMAWARE_ALLOW_DIRTY:-0}" != "1" ]]; then
    echo "Refusing to build: the image inputs differ from ${git_sha}." >&2
    echo "Tagging this build with that SHA would make the tag a lie, and the" >&2
    echo "SHA short-circuit would then hand the stale image to every later run." >&2
    echo "${dirty}" >&2
    echo "Commit the changes, or set FIRMAWARE_ALLOW_DIRTY=1 for a content-tagged build." >&2
    exit 3
  fi
  git_sha="$(content_tag)"
  echo "[build] dirty tree: building as ${git_sha} rather than a commit SHA" >&2
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
  --build-arg PIP_EXTRAS="${extras}" \
  --target "${target}" \
  .

digest="$(az acr repository show \
  --name "${registry}" \
  --image "${repository}:${git_sha}" \
  --query digest -o tsv)"
emit "${digest}"
