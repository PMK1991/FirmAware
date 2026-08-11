# FirmAware GCP deployment

Terraform root for the GCP target. The Azure target lives in
[`../azure`](../azure) and has separate state; the two share no modules by
design. Scripts referenced below live in `deploy/gcp/`.

This directory deploys the batch-only FirmAware pipeline as three Cloud Run
Jobs. Terraform owns all resources except the remote-state bucket and Workload
Identity Pool, which `bootstrap.sh` creates idempotently.

## Decisions

| Topic | Decision |
|---|---|
| Environment isolation | Dev and prod use separate GCP projects. This isolates IAM, state, buckets, quotas, alerts, and accidental deletion. |
| Image promotion | Dev builds once. Production pulls the image live in dev and pushes that local manifest to prod Artifact Registry without rebuilding; the workflow rejects a changed SHA-256 digest. |
| Immutable tags | Artifact Registry has immutable tags, so builds use only the Git commit SHA. The requested floating `latest-dev` tag is intentionally omitted because it cannot move in an immutable-tag repository. |
| Scores IAM | Runtime receives `objectCreator` + `objectViewer`, not `objectAdmin`, because `objectAdmin` includes delete and conflicts with the required negative delete test. |
| Bucket deletion protection | Provider 7's `deletion_policy` is `PREVENT` in prod and `DELETE` in dev. All buckets also use `force_destroy = false`. |
| GCS artifact prefix | `FIRMAWARE_ARTIFACTS_URI` is the artifacts bucket root. Runs live under `runs/{run_id}/`; `champion.json` is at the root. |
| GCS scores prefix | `FIRMAWARE_SCORES_URI` is `gs://firmaware-{env}-scores/scores`; each prediction creates a new object. |
| MLflow in Cloud Run | MLflow's SQLite store is ephemeral under `/tmp`. GCS run folders and `champion.json` are the GCP system of record; no MLflow server is deployed. |
| First deployment | Foundation must exist before its first image can be pushed. Bootstrap therefore uses the two-stage foundation/build/full-apply sequence below. Subsequent deploys are one plan/apply. |
| Alert email | Empty in dev; required in prod. GCP may email the recipient to confirm a newly created notification channel. |
| Resource labels | Labels are applied wherever the GCP resource schema supports them. IAM bindings, service accounts, enabled APIs, object uploads, and notification policies do not expose resource labels. |
| CI Terraform permissions | A full Terraform apply needs resource-specific admin roles for APIs, Artifact Registry, Scheduler, service accounts/WIF, logging, monitoring, project IAM, Cloud Run, and storage. The deployer has those roles but no primitive Owner/Editor role; GitHub environment protection is the approval boundary. |

## Resource topology

Each environment project contains:

- Regional Artifact Registry repository `firmaware` with immutable tags.
- `firmaware-{env}-data`: uniform access, versioning, noncurrent versions
  removed after 90 days.
- `firmaware-{env}-artifacts`: the same controls plus immutable model runs and
  the atomic champion pointer.
- `firmaware-{env}-scores`: no expiration lifecycle; score objects are
  append-only.
- Three Cloud Run Jobs named `firmaware-{env}-{validate|train|predict}`.
- Nightly Cloud Scheduler job invoking only the predict job.
- Log metrics and an alert policy for execution failures and contract errors.
- Jobs, deployer, and scheduler service accounts with no user-managed keys.

## Live environments

| Setting | dev | prod |
|---|---|---|
| GCP project | `firmaware` (number `558685262335`) | _not provisioned_ |
| Region | `us-central1` | _not provisioned_ |
| Terraform state bucket | `firmaware-558685262335-tf-state`, prefix `firmaware/dev` | _not provisioned_ |
| WIF provider | `projects/558685262335/locations/global/workloadIdentityPools/github-actions/providers/github` | _not provisioned_ |
| Deployer service account | `sa-firmaware-deployer@firmaware.iam.gserviceaccount.com` | _not provisioned_ |
| Schedule | `0 2 * * *` `Etc/UTC`, enabled | _not provisioned_ |
| Alert email | `data4v@gmail.com` | _not provisioned_ |

GitHub repository variables `GCP_REGION`, `DEV_GCP_PROJECT_ID`,
`DEV_WORKLOAD_IDENTITY_PROVIDER`, and `DEV_DEPLOYER_SERVICE_ACCOUNT` are set at
repository scope; `DEV_TF_STATE_BUCKET` is set on the `development` environment.
Adding prod means repeating the bootstrap in a second project and setting the
matching `PROD_*` variables on the `production` environment.

## Bootstrap

Prerequisites:

- An existing GCP project with billing enabled for each environment.
- `gcloud`, Terraform >= 1.7, Docker, and permission to enable APIs and manage
  project IAM during baseline creation.
- GitHub repository environments named `development` and `production`.
  Configure required reviewers on `production`.

Authenticate as the one-time platform administrator:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project "$DEV_GCP_PROJECT_ID"
```

Bootstrap state and the GitHub WIF pool:

```bash
export GCP_PROJECT_ID="$DEV_GCP_PROJECT_ID"
# Bucket names are globally unique; the project number keeps this collision-free.
project_number="$(gcloud projects describe "$DEV_GCP_PROJECT_ID" --format='value(projectNumber)')"
export TF_STATE_BUCKET="${DEV_GCP_PROJECT_ID}-${project_number}-tf-state"
bash infra/gcp/bootstrap.sh "$DEV_GCP_PROJECT_ID" PMK1991 FirmAware
```

The pool is external to Terraform. Terraform creates the repo-scoped provider,
deployer service account, and impersonation binding. No service-account key is
created.

### First environment apply

Artifact Registry must exist before Docker can push the first job image:

```bash
terraform -chdir=infra init \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="prefix=firmaware/dev"

# Create enabled APIs and the registry first.
terraform -chdir=infra apply \
  -target=module.foundation \
  -var-file=envs/dev.tfvars \
  -var="project_id=$DEV_GCP_PROJECT_ID" \
  -var="state_bucket_name=$TF_STATE_BUCKET"

export GCP_PROJECT_ID="$DEV_GCP_PROJECT_ID"
image_digest="$(bash deploy/gcp/build.sh dev | sed -n 's/^image_digest=//p')"

terraform -chdir=infra apply \
  -var-file=envs/dev.tfvars \
  -var="project_id=$DEV_GCP_PROJECT_ID" \
  -var="state_bucket_name=$TF_STATE_BUCKET" \
  -var="image_digest=$image_digest"
```

Upload the real inputs and deliberately create the first champion:

```bash
gcloud storage cp data/deployment_events.csv \
  "gs://firmaware-dev-data/deployment_events.csv"
gcloud storage cp data/upcoming_deployments.csv \
  "gs://firmaware-dev-data/upcoming_deployments.csv"
bash deploy/gcp/run_job.sh validate dev
bash deploy/gcp/run_job.sh train dev
bash deploy/gcp/smoke_test.sh dev
```

The first prod baseline is deliberate because an automated smoke test needs a
champion before the first tag promotion:

1. Run `bootstrap.sh` in the prod project and apply only `module.foundation`.
2. Pull the digest currently live in dev, retag that local manifest for the
   prod `firmaware` repository, push it, and verify the SHA-256 is unchanged.
3. Apply full prod Terraform with `scheduler_enabled=false`.
4. Upload prod inputs, execute validate, and deliberately execute train once.
5. Run `smoke_test.sh prod`; then reapply with `scheduler_enabled=true`.

After that baseline, every `v*` tag uses the approval-gated workflow and never
rebuilds the image.

### Idempotence check

After the full apply, repeat the same plan:

```bash
terraform -chdir=infra plan \
  -detailed-exitcode \
  -var-file=envs/dev.tfvars \
  -var="project_id=$DEV_GCP_PROJECT_ID" \
  -var="state_bucket_name=$TF_STATE_BUCKET" \
  -var="image_digest=$image_digest"
```

Exit code `0` means no changes. Exit code `2` is a diff and must be resolved
before CI is enabled.

## GitHub configuration

Set repository or GitHub environment variables:

| Variable | Scope |
|---|---|
| `GCP_REGION` | Repository |
| `DEV_GCP_PROJECT_ID` | Repository (dev deploy and prod promotion both read it) |
| `DEV_WORKLOAD_IDENTITY_PROVIDER`, `DEV_DEPLOYER_SERVICE_ACCOUNT` | Repository |
| `DEV_TF_STATE_BUCKET` | Development |
| `PROD_GCP_PROJECT_ID`, `PROD_TF_STATE_BUCKET` | Production |
| `PROD_WORKLOAD_IDENTITY_PROVIDER`, `PROD_DEPLOYER_SERVICE_ACCOUNT` | Production |

The provider value has this shape:

```text
projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-actions/providers/github
```

The workflows use OIDC and service-account impersonation. The dev provider
accepts only `main` in the `development` environment plus approved `v*`
promotion jobs in `production`. The prod provider accepts only `v*` tags in
the `production` environment. Do not create or add JSON keys to GitHub secrets.

## Operations

```bash
bash deploy/gcp/run_job.sh validate dev
bash deploy/gcp/run_job.sh train dev
bash deploy/gcp/run_job.sh predict dev
bash deploy/gcp/smoke_test.sh dev
```

Training is never triggered by a merge. It is a separate audited operation
because it can change model behavior without changing application code.

The dev workflow starts only after CI succeeds for the exact merged `main`
SHA, and reruns tests before deployment. It applies the candidate with the
scheduler paused, validates and smoke-tests it, and either restores the prior
digest or enables the schedule and writes `deployments/live.json`. A `v*` tag
enters the GitHub `production` approval gate, requires the tag commit and live
dev image to match that success record, copies the manifest to prod without
rebuilding, verifies equal digest, and follows the same paused/smoke/activate
sequence.

## Rollback runbook

### Model

List immutable runs:

```bash
bash deploy/gcp/rollback_model.sh dev
```

Promote a prior run with a generation precondition on `champion.json`:

```bash
bash deploy/gcp/rollback_model.sh dev RUN_ID
bash deploy/gcp/run_job.sh predict dev
```

Target: under 2 minutes. Verify the new score object's `model_run` matches the
restored run metadata timestamp.

### Code/image

Use a prior digest from a successful deploy summary or the table below:

```bash
export DEV_GCP_PROJECT_ID=...
export DEV_TF_STATE_BUCKET=...
bash deploy/gcp/rollback_image.sh dev \
  us-central1-docker.pkg.dev/PROJECT/firmaware/firmaware@sha256:DIGEST
```

Target: under 10 minutes. Cloud Run Jobs remain pinned to the previous digest
until Terraform succeeds.

### Infrastructure

Revert the bad infrastructure commit, merge it, and let the environment
workflow reapply Terraform. If Terraform state itself is corrupt, restore a
prior generation from the versioned state bucket before planning.

Target: under 30 minutes.

### Data

List generations, restore the selected generation, and validate:

```bash
gcloud storage ls --all-versions \
  gs://firmaware-dev-data/deployment_events.csv
current_generation="$(gcloud storage objects describe \
  gs://firmaware-dev-data/deployment_events.csv \
  --format='value(generation)')"
gcloud storage cp \
  gs://firmaware-dev-data/deployment_events.csv#GENERATION \
  gs://firmaware-dev-data/deployment_events.csv \
  --if-generation-match="$current_generation"
bash deploy/gcp/run_job.sh validate dev
```

Target: under 10 minutes.

Scores are intentionally not rolled back or deleted. Publish a corrected run
and record the superseded score object in the incident table.

## Required negative checks

No user-managed service-account keys:

```bash
for sa in sa-firmaware-jobs sa-firmaware-deployer sa-firmaware-scheduler; do
  gcloud iam service-accounts keys list \
    --iam-account="${sa}@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
    --filter='keyType=USER_MANAGED'
done
```

The runtime identity can create but cannot delete scores:

```bash
jobs_sa="sa-firmaware-jobs@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
gcloud storage rm gs://firmaware-dev-scores/scores/KNOWN_OBJECT.csv \
  --impersonate-service-account="$jobs_sa"
# Expected: permission storage.objects.delete denied.
```

Contract-trip check:

1. Upload a training CSV containing outcome `ROLLBACK_REQUIRED`.
2. Execute validate or train.
3. Confirm exit code 1 in the execution and the monitoring alert.
4. Restore the prior data object generation.

## Cost estimate

Assumptions per environment: one prediction daily, 55 rows, one occasional
15-minute training run monthly, under 5 GB across buckets/registry, and low log
volume.

| Component | Estimated monthly cost |
|---|---:|
| Cloud Run Jobs | < $1.50 |
| Cloud Scheduler | about $0.10 |
| GCS data, artifacts, scores, and state | < $0.50 |
| Artifact Registry | < $0.25 |
| Logging and Monitoring | < $0.50 |
| **Estimated total** | **< $2.85/environment** |

This excludes GitHub-hosted runner usage and unusually frequent training. It is
well below the $15/environment target.

## Deployment history (last 10)

CI prints the immutable digest and champion run to the GitHub step summary.
Copy approved production entries here during release review.

| Date | Environment | Git ref | Image digest | Champion run | Result |
|---|---|---|---|---|---|
| 2026-07-31 | dev | `4e0b4ec` | `sha256:fbedf3379857485e310ad4d246a0327325649228ae72ad89c5b664de2279ead1` | `6a2366e506e74888ae7d246cb0925126` | Success |

## Rollback rehearsal record

| Date | Environment | Class | Observed time | Evidence | Result |
|---|---|---|---:|---|---|
| 2026-07-31 | dev | Model | 37s | `rollback_model.sh dev 6a2366e5…` restored `champion.json` to metadata digest `8e214c16…` under a generation precondition | Pass (target < 2 min) |
| 2026-07-31 | dev | Code/image | 42s | `rollback_image.sh dev …@sha256:fbedf337…` reapplied Terraform and repinned all three jobs | Pass (target < 10 min) |
| 2026-07-31 | dev | Infrastructure | 31s | `terraform plan -detailed-exitcode` returned 0 (no drift) after apply; reapply from known-good config converges | Pass (target < 30 min) |
| 2026-07-31 | dev | Data | 11s | Uploaded `deployment_outcome=ROLLBACK_REQUIRED`; validate exited 1; restored prior generation `1785516870352800` with `--if-generation-match`; validate exited 0 and MD5 matched source | Pass (target < 10 min) |

## Contract-trip verification

| Date | Environment | Injected fault | Job | Exit code | Result |
|---|---|---|---|---:|---|
| 2026-07-31 | dev | 3 rows with `deployment_outcome=ROLLBACK_REQUIRED` | `firmaware-dev-validate` | 1 | Pass |

## Negative check record

| Date | Environment | Check | Expected | Observed | Result |
|---|---|---|---|---|---|
| 2026-07-31 | dev | User-managed keys on jobs/deployer/scheduler service accounts | none | none | Pass |
| 2026-07-31 | dev | Jobs identity deletes a scores object | denied | `storage.objects.delete` denied | Pass |
| 2026-07-31 | dev | Jobs identity reads a scores object | allowed | header and rows returned | Pass |
| 2026-07-31 | dev | Jobs identity writes to the data bucket | denied | permission denied | Pass |

## Incident record

| Date | Environment | Bad run/object | Superseding run/object | Notes |
|---|---|---|---|---|
| _none_ | | | | |
