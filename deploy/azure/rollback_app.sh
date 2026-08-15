#!/usr/bin/env bash
# Page rollback: a traffic flip, nothing else.
#
# The previous revision was never deactivated and is still provisioned, so this
# is one control-plane call and no container start. That is the entire reason
# promote_app.sh keeps it at 0% rather than tearing it down, and it is what makes
# the sub-30-second target real rather than aspirational.
#
# The target is resolved from the app rather than assumed, for the same reason
# rollback_endpoint.sh resolves its slot: an incident is the worst moment to
# discover that a hardcoded name was wrong.
set -euo pipefail

env_name="${1:?usage: rollback_app.sh <dev|prod> [revision]}"
target="${2:-}"

resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
container_app="ca-firmaware-${env_name}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

az_app() { az containerapp "$@" --name "${container_app}" --resource-group "${resource_group}"; }

traffic="$(az_app show --query "properties.configuration.ingress.traffic" -o json)"
live="$(python3 -c '
import json, sys
entries = json.loads(sys.argv[1] or "[]") or []
best = max(entries, key=lambda item: item.get("weight", 0), default=None)
print("" if best is None else (best.get("revisionName") or ""))
' "${traffic}")"

if [[ -z "${target}" ]]; then
  # The most recently created active revision that is not the one currently
  # serving. Active, because a deactivated revision cannot take traffic and
  # routing to it would turn a rollback into an outage.
  target="$(az_app revision list \
    --query "sort_by([?properties.active], &properties.createdTime)[].name" -o tsv \
    | grep -vx "${live:-__none__}" | tail -1 || true)"
fi

[[ -n "${target}" ]] || { echo "no revision to roll back to: ${live:-nothing} is the only active one" >&2; exit 1; }

state="$(az_app revision show --revision "${target}" \
  --query "properties.runningState" -o tsv 2>/dev/null || echo "Missing")"
case "${state}" in
  # Scaled counts. A revision at 0% traffic with min_replicas 0 has legitimately
  # scaled to nothing, which is what "warm rollback target" looks like in a
  # scale-to-zero environment -- refusing it would rule out the only candidate.
  Running|Scaled|RunningAtMaxScale) ;;
  *) echo "cannot roll back: ${target} is ${state}" >&2; exit 1 ;;
esac

if [[ -n "${live}" && "${live}" != "${target}" ]]; then
  weights=("${target}=100" "${live}=0")
else
  weights=("${target}=100")
fi

az_app ingress traffic set --revision-weight "${weights[@]}" --output none

after="$(az_app show \
  --query "properties.configuration.ingress.traffic[?revisionName=='${target}'].weight | [0]" -o tsv)"
[[ "${after}" == "100" ]] || { echo "rollback did not take: ${target}=${after}" >&2; exit 1; }

echo "[rollback-app] ${target} is serving 100%; traffic map is now: ${weights[*]}"
