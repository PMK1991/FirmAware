# FirmAware

FirmAware is a deterministic firmware deployment-risk pipeline with
time-aware hyperparameter tuning, detailed MLflow experiment tracking, model
registry publication, and a raw-input model package that can be served locally
or moved to Azure ML and GCP.

## Setup

Python 3.11 or newer is required. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Place `deployment_events.csv` and `upcoming_deployments.csv` in `data\`.
Training input has the 30-column schema in the specification. Scoring input
omits `deployment_outcome`, `time_to_failure_hours`, and `rollback_required`.

## Quickstart

```powershell
python -m firmaware validate --input data\deployment_events.csv --mode training
python -m firmaware train --input data\deployment_events.csv --config config.yaml
python -m firmaware predict --input data\upcoming_deployments.csv
```

The default training run creates `mlflow.db`, stores MLflow artifacts in
`mlruns\`, registers the champion as `FirmAwareRiskModel`, and atomically
publishes the latest local batch artifacts under `artifacts\`.

Open the experiment UI:

```powershell
mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000
```

The run and registered-model URIs are printed after training and persisted in
`artifacts\metadata.json`.

## Tuning and evaluation design

FirmAware uses strict chronological model selection:

1. The outer split reserves the latest data as the family-selection holdout.
2. The earlier side becomes four expanding time folds. Every fold trains only
   on dates earlier than its validation dates.
3. Every configured logistic-regression and XGBoost candidate runs across all
   folds as one nested MLflow trial.
4. Hyperparameters and one cost-sensitive threshold are selected from pooled
   out-of-time predictions, with ROC-AUC as the tie-breaker.
5. The best candidate from each family is trained on the full outer training
   side. The holdout expected-cost comparison chooses the published family,
   matching the FirmAware champion contract.

The default deterministic randomized search evaluates 12 logistic and 30
XGBoost candidates. It covers logistic regularization/class weighting plus
XGBoost depth, estimators, learning rate, child weight, row/feature sampling,
gamma, and L1/L2 regularization. Edit `tuning.search_spaces` and
`tuning.max_candidates` to change the budget. Set `tuning.strategy: grid` for
exhaustive enumeration or `tuning.enabled: false` for the single parameter sets
under `models`.

The parent MLflow run includes:

- Accuracy, precision, recall, specificity, F1, ROC-AUC, average precision,
  Brier score, log loss, confusion counts, false-positive/negative rates, and
  cost-weighted expected cost.
- Nested tuning and final-family runs with parameters, thresholds, and metrics.
- `evaluation_report.html`, all tuning results, final model comparison,
  confusion matrix, classification report, threshold diagnostics, ROC and
  precision-recall curve data, and feature importance.
- The fitted raw-input PyFunc model, explicit nullable-numeric signature,
  dependency versions, source package, input example, and registry version.

Row-level test predictions are disabled by default to avoid publishing
deployment identifiers. Set `mlflow.log_row_level_artifacts: true` only when
the tracking server has the appropriate access controls. The model input
example is synthetic and tracking URIs are redacted before metadata is logged.

## Serving the registered model

The MLflow model accepts the same 27 columns as
`upcoming_deployments.csv`. Validation, feature derivation, imputation,
encoding, scaling, OOD reporting, probability scoring, and decision generation
are included in one PyFunc package.

```powershell
mlflow models serve -m "models:/FirmAwareRiskModel/1" -p 5001 --env-manager local
```

Build a portable Linux container when the target platform expects a custom
image:

```powershell
mlflow models build-docker -m "models:/FirmAwareRiskModel/1" -n firmaware-model
```

## Azure ML and GCP

No provider SDK is imported by the pipeline. Standard MLflow environment
variables redirect the same training command:

```powershell
$env:MLFLOW_TRACKING_URI = "https://your-mlflow-tracking-endpoint"
$env:MLFLOW_EXPERIMENT_NAME = "firmaware-production"
$env:FIRMAWARE_REGISTERED_MODEL_NAME = "FirmAwareRiskModel"
python -m firmaware train --input data\deployment_events.csv --config config.yaml
```

For **Azure ML**, install `azureml-mlflow` in the Azure job image and set
`MLFLOW_TRACKING_URI` to the workspace MLflow tracking URI. Azure identity
supplies authentication; the run, artifacts, and registered PyFunc model are
published to the workspace.

For **GCP**, point `MLFLOW_TRACKING_URI` at a standard MLflow server on Cloud
Run or GKE backed by PostgreSQL. Configure that server with a `gs://` artifact
destination and workload identity. If a training job talks directly to a
database-backed tracking store instead of an HTTP server, set
`FIRMAWARE_MLFLOW_ARTIFACT_ROOT=gs://your-bucket/path`.

When tracking through a remote HTTP or managed endpoint, FirmAware leaves
artifact routing to the server unless an explicit cloud artifact URI is set.
Standard MLflow authentication variables remain outside config and metadata.

## Local batch output

Prediction uses the persisted champion and pooled-CV-selected threshold, then
appends rows to `outputs\scores.csv`. Unknown categorical values are encoded as
all-zero one-hot blocks and printed as prominent warnings while still being
scored. Existing score history is never overwritten.

## Decisions where the specification was silent

| Decision | Implementation |
|---|---|
| Quantile boundary with repeated dates | The boundary date starts the later side; all rows on that date move together so chronology is strict. |
| Threshold cost tie | Keep the first threshold in the ascending `0.01`–`0.99` sweep. |
| Complete tuning tie | Prefer higher ROC-AUC, then logistic regression and deterministic parameter JSON order. |
| Calibration slice | Use the latest 20% of the relevant training side and require at least 20 rows plus both classes. |
| Entirely missing training numeric column | Fail clearly because no train-fitted median exists. |
| Decision precision | Apply the threshold to the unrounded probability, then serialize the reported probability to exactly four decimal places. |
| Existing incompatible scores file | Fail rather than overwrite or append under a mismatched header. |
| Local artifact publication | Stage by MLflow run ID and promote only after tracking and registry publication succeed. |

Class weighting is optional in the logistic search despite the near-balanced
source classes. The final shipped family winner is not retrained after its
holdout comparison.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Verified output on 2026-07-22:

```text
Ran 12 tests in 18.901s

OK
```

The suite covers the data contract, label behavior, signed version features,
persisted transform parity, OOD encoding, strict outer and rolling time splits,
deterministic metrics, leakage exclusion, append-only output, MLflow nested
tracking, registry publication, detailed evaluation artifacts, hosted/local
prediction parity, and hosted inference with nullable numeric values.
