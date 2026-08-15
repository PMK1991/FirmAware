#!/usr/bin/env bash
# Create a new revision of the page at ZERO traffic.
#
# The same split the ML endpoint uses, for the same reason: Terraform owns the
# container app, and a revision is a release. Nothing here moves traffic. The new
# revision is fully provisioned and smoke tested on its own hostname while the
# live one keeps serving every visitor, and promotion is a separate, explicit
# call. That separation is what makes rollback a traffic update rather than a
# rebuild, and it is why rollback_app.sh is a single control-plane call.
#
# One non-obvious step happens first. Terraform creates the app with a traffic
# map of `latestRevision: true`, because on the very first release there is no
# revision to name yet. Left that way, the next revision would take 100% of the
# traffic the instant it was created -- before anything had tested it, which is
# precisely the failure this script exists to prevent. So the live revision is
# pinned by name before the new one is built.
set -euo pipefail

env_name="${1:?usage: deploy_app.sh <dev|prod> <image_digest>}"
image_digest="${2:?image digest is required: tags are mutable, digests are not}"

resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
app="firmaware-${env_name}"
container_app="ca-${app}"
git_sha="${FIRMAWARE_GIT_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi
if [[ ! "${image_digest}" =~ @sha256:[0-9a-f]{64}$ ]]; then
  echo "image_digest must be a full <registry>/<repo>@sha256:<64 hex> reference" >&2
  exit 2
fi

az_app() { az containerapp "$@" --name "${container_app}" --resource-group "${resource_group}"; }

# A revision suffix may only be lowercase alphanumerics and dashes, and must not
# end in one. A short SHA satisfies that; a content tag from a dirty local build
# ("dirty-<hex>") does too.
suffix="$(printf '%s' "${git_sha}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-10 | sed 's/-*$//')"
[[ -n "${suffix}" ]] || { echo "could not derive a revision suffix from ${git_sha}" >&2; exit 2; }

# --- pin the live revision by name -------------------------------------------

traffic_before="$(az_app show --query "properties.configuration.ingress.traffic" -o json)"

live="$(python3 -c '
import json, sys
traffic = json.loads(sys.argv[1] or "[]") or []
# Whichever entry carries the most traffic is live. `latestRevision` entries have
# no revisionName, which is exactly the case this script has to resolve.
best = max(traffic, key=lambda item: item.get("weight", 0), default=None)
print("" if best is None else (best.get("revisionName") or "@latest"))
' "${traffic_before}")"

if [[ "${live}" == "@latest" ]]; then
  # Resolve what "latest" currently means, so it can be named. Only revisions
  # that are actually running are candidates: pinning traffic to a failed
  # revision would take the page down as the first act of a deploy.
  live="$(az_app revision list \
    --query "sort_by([?properties.active && properties.runningState=='Running'], &properties.createdTime)[-1].name" \
    -o tsv)"
  if [[ -z "${live}" || "${live}" == "None" ]]; then
    echo "[deploy-app] no running revision to pin; this is the first release" >&2
    live=""
  else
    echo "[deploy-app] pinning live traffic to ${live} before creating the new revision"
    az_app ingress traffic set --revision-weight "${live}=100" --output none
  fi
fi

echo "[deploy-app] live revision: ${live:-none}; creating: ${container_app}--${suffix}"

# --- create the new revision --------------------------------------------------

# Idempotent in the way that matters: rerunning the same commit produces the same
# revision name, and Container Apps updates that revision rather than piling up
# duplicates. --container-name is explicit so this keeps working if a sidecar is
# ever added.
az_app update \
  --image "${image_digest}" \
  --container-name page \
  --revision-suffix "${suffix}" \
  --output none

revision="${container_app}--${suffix}"

# --- assert nothing moved -----------------------------------------------------

traffic_after="$(az_app show --query "properties.configuration.ingress.traffic" -o json)"
python3 -c '
import json, sys
after = json.loads(sys.argv[1] or "[]") or []
revision = sys.argv[2]
first_release = sys.argv[3] == "1"
weight = next(
    (item.get("weight", 0) for item in after if item.get("revisionName") == revision),
    0,
)
latest = next((item for item in after if item.get("latestRevision")), None)
if first_release:
    # Nothing was serving before, so the new revision taking traffic is the
    # intended outcome rather than a premature promotion.
    raise SystemExit(0)
if weight or latest is not None:
    raise SystemExit(
        f"deploy shifted traffic to {revision}: {after}; "
        "a new revision must stay at 0% until the smoke test passes"
    )
' "${traffic_after}" "${revision}" "$([[ -z "${live}" ]] && echo 1 || echo 0)"

fqdn="$(az_app revision show --revision "${revision}" --query "properties.fqdn" -o tsv)"

echo "revision=${revision}"
echo "previous_revision=${live}"
echo "revision_fqdn=${fqdn}"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "revision=${revision}"
    echo "previous_revision=${live}"
    echo "revision_fqdn=${fqdn}"
  } >> "${GITHUB_OUTPUT}"
fi
echo "[deploy-app] ${revision} is provisioned at 0% traffic on https://${fqdn}"
