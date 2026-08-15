#!/usr/bin/env bash
# Move the page's traffic to a revision that has already passed its smoke test.
#
# Unlike the ML endpoint there is no 10/50/100 ramp here, and that is a
# deliberate difference rather than a shortcut. The endpoint's ramp exists to
# limit how many real scoring requests a bad deployment can answer wrongly; this
# is a read-only page whose failure mode is that it does not render, which the
# smoke test has already proven it does. A partial split would only mean some
# visitors get the old page and some the new, with no way to tell which they saw.
#
# The previous revision is left provisioned at 0%. It is not garbage: it is the
# rollback target, and keeping it warm is what makes rollback_app.sh a single
# control-plane call instead of a container start.
set -euo pipefail

env_name="${1:?usage: promote_app.sh <dev|prod> <revision> [previous_revision]}"
revision="${2:?revision is required}"
previous="${3:-}"

resource_group="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
container_app="ca-firmaware-${env_name}"

if [[ ! "${env_name}" =~ ^(dev|prod)$ ]]; then
  echo "Environment must be dev or prod" >&2
  exit 2
fi

az_app() { az containerapp "$@" --name "${container_app}" --resource-group "${resource_group}"; }

state="$(az_app revision show --revision "${revision}" \
  --query "properties.runningState" -o tsv 2>/dev/null || echo "Missing")"
case "${state}" in
  Running|Scaled|RunningAtMaxScale) ;;
  *) echo "refusing to promote ${revision}: running state is ${state}" >&2; exit 1 ;;
esac

if [[ -n "${previous}" && "${previous}" != "${revision}" ]] \
  && az_app revision show --revision "${previous}" >/dev/null 2>&1; then
  weights=("${revision}=100" "${previous}=0")
else
  weights=("${revision}=100")
fi

echo "[promote-app] ${weights[*]}"
az_app ingress traffic set --revision-weight "${weights[@]}" --output none

after="$(az_app show \
  --query "properties.configuration.ingress.traffic[?revisionName=='${revision}'].weight | [0]" -o tsv)"
[[ "${after}" == "100" ]] || { echo "promotion did not take: ${revision}=${after}" >&2; exit 1; }

# Two revisions are kept: the one serving and the one to fall back to. Anything
# older is deactivated -- it can serve no traffic and cannot be rolled back to
# without a decision nobody would make under pressure, and in prod each idle
# revision still holds addresses out of the apps subnet.
mapfile -t stale < <(az_app revision list \
  --query "[?properties.active].name" -o tsv \
  | grep -vx -e "${revision}" -e "${previous:-__none__}" || true)

for old in "${stale[@]}"; do
  [[ -n "${old}" ]] || continue
  echo "[promote-app] deactivating superseded revision ${old}"
  az_app revision deactivate --revision "${old}" --output none || true
done

fqdn="$(az_app show --query "properties.configuration.ingress.fqdn" -o tsv)"
echo "[promote-app] ${revision} is serving 100% on https://${fqdn}"
echo "[promote-app] rollback target: ${previous:-none, this is the first release}"
