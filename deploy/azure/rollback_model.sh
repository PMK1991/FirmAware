#!/usr/bin/env bash
# Batch model rollback: re-point the batch endpoint's default deployment at an
# earlier registry version.
#
# Batch cannot roll back the way online does. There is no warm second slot to
# flip traffic to, so this creates (or reuses) a deployment pinned to the
# requested version and makes it the default. That is why the architecture gives
# it a five-minute target rather than the online path's thirty seconds.
#
# That is also exactly what a forward release does, so the mechanics live in
# deploy_batch.sh and both paths call it. Keeping a private copy here would mean
# the rollback path was the only one that ever created a deployment, leaving the
# code an incident depends on as the code least often run.
#
# Scores already written are never touched. They are append-only evidence under a
# platform immutability policy: a superseded score stays, and the corrected run
# is written alongside it. Rolling back the model does not rewrite history.
set -euo pipefail

env_name="${1:?usage: rollback_model.sh <dev|prod> <model_version>}"
model_version="${2:?model version is required}"

bash deploy/azure/deploy_batch.sh "${env_name}" "${model_version}"

echo "[rollback] previously written scores are unchanged and remain immutable"
