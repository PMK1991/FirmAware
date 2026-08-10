# Azure deployment scripts and Azure ML assets

Cloud-specific code for the Azure target. The shared application in
`src/firmaware/` is untouched and stays cloud-agnostic — that portability is the
reason one image runs on both clouds.

| Path | Purpose |
|---|---|
| `azureml/environment.yaml` + `conda.yaml` | Registered environment: the GCP image by digest, plus `azureml-mlflow` so existing MLflow calls target the workspace |
| `azureml/pipeline-train.yaml` | validate → features → train → evaluate+gate → register |
| `azureml/components/` | One component per pipeline step (to be written) |
| `azureml/endpoint-online.yaml`, `deployment-{blue,green}.yaml` | Managed Online Endpoint and its two deployments |
| `azureml/endpoint-batch.yaml` | Scheduled bulk scoring |
| `azureml/score.py` | Online scoring entry script — 422 on contract violation, always reports `unseen_categories` |
| `run_training.sh` | Submit the pipeline, stream, return run id |
| `promote_traffic.sh` | Staged 10 → 50 → 100 with a health check between steps |
| `rollback_endpoint.sh` | Traffic flip to blue — the sub-30-second rollback |
| `security_check.sh` | Asserts the spec's non-negotiables against live Azure state |

## MLflow on Azure

MLflow is the tracking and registry interface on both clouds, which is what
keeps the application code identical:

| Concern | Local | Azure |
|---|---|---|
| Tracking | SQLite `mlflow.db` | workspace MLflow endpoint (`MLFLOW_TRACKING_URI`) |
| Artifacts | `mlruns/` | workspace-managed storage |
| Registry | local registry | workspace registry, `models:/FirmAwareRiskModel/{version}` |
| Auth | none | managed identity — no keys |

The only change is the tracking URI. Training calls the same `mlflow` functions
it calls locally; `register` publishes the PyFunc; deployments reference that
registered version, so the served artefact packages validation, preprocessing,
scoring, and decision generation with no second preprocessing path.

GCP deliberately runs **without** an MLflow server (GCS metadata plus
`champion.json` is its system of record). Azure needs no server either — the
workspace provides managed tracking. Neither deployment hosts one.

## Status

Scaffolding only. Terraform in `infra/azure/` is not written yet; the build
order and acceptance criteria are in
[`docs/design/azure-implementation-spec.md`](../../docs/design/azure-implementation-spec.md).
