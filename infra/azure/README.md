# FirmAware Azure deployment

Terraform root for the Azure target. The GCP target lives in
[`../gcp`](../gcp) and has separate state; the two share no modules by design.
Scripts referenced below live in `deploy/azure/`, and the Azure ML job and
deployment specs in `deploy/azure/azureml/`.

This directory deploys the FirmAware pipeline as an Azure ML workspace with a
training pipeline, a batch endpoint, and an optional blue/green online endpoint.
Terraform owns every resource except the remote-state account and the CI
federated identity, which `bootstrap.sh` creates idempotently.

The design this implements is [`../../docs/design/azure-architecture.md`](../../docs/design/azure-architecture.md);
the contract it is built against is [`../../docs/design/azure-implementation-spec.md`](../../docs/design/azure-implementation-spec.md).

## Decisions

| Topic | Decision |
|---|---|
| Environment isolation | Dev and prod are separate resource groups with separate Terraform state, separate federated credentials, and separate registries. A leaked dev token cannot address prod, because each credential's OIDC subject names one GitHub environment and prod's is gated on required reviewers. |
| Release identity | The image **digest**, never a tag. `build.sh` emits `repo@sha256:…` and every downstream consumer — the AML environment, both deployments, the prod promotion check — refers to that. A tag can be moved between plan and apply; a digest cannot. |
| Image promotion | Prod does not rebuild. `azure-deploy-prod.yaml` takes a digest as input, proves that digest is currently serving traffic in dev, then `az acr import`s those exact bytes. A rebuild from the same commit can differ (base image moved, transitive pin re-resolved), so "same commit" is a weaker claim than "same bytes". |
| Blue/green slot selection | Computed from the endpoint's live traffic map, never assumed. `deploy_endpoint.sh` targets whichever slot carries least traffic, ties going to green so the first deploy lands on blue. "Green is always the new one" is correct exactly once and wrong on every release after that. |
| Model registration | The pipeline sets `mlflow.register_model: false` via `config.azure.yaml` and registers from a dedicated `register` component that runs *after* the gate. If training registered its own model, a gate failure would still leave a version in the registry, and acceptance criterion 5 requires that it does not. |
| Gate baseline | Read from the registry at runtime, not hardcoded. `evaluate_step.py` takes the incumbent's `test_roc_auc` tag as the bar to beat and falls back to the configured `auc_floor` only when the registry is empty. This is why `register_step.py` tags every test metric onto the version. |
| Feature handling in the pipeline | `train` re-derives features from raw rows rather than consuming the `features` component's matrix. Serving derives features the same way, so handing training a pre-computed matrix would create train/serve skew. The `features` output exists for inspection and for the DAG edge, not as training input. |
| Endpoint ownership | Terraform creates the endpoints (`azapi`), the release scripts create the *deployments*. Infrastructure lifetime and release lifetime are different; an endpoint outlives every deployment that has ever sat behind it. `endpoint-online.yaml` and `endpoint-batch.yaml` are the reviewable declarations of what Terraform builds. |
| Score immutability | Scores are append-only by two independent mechanisms: an RBAC role with no `blobs/delete` action, and a container immutability policy. Either alone is a single point of failure — RBAC can be re-granted, a policy can be created unlocked — so both are present. |
| MLflow | The workspace's native tracking is the system of record. No MLflow server is deployed, and the local SQLite store stays a development convenience, exactly as on GCP. |
| MLflow version | Pinned to **2.x** (`mlflow==2.22.5`), and this is an Azure constraint rather than caution. The workspace tracking server implements the MLflow 2 REST surface and has no `/api/2.0/mlflow/logged-models`, while MLflow 3's `Model.log()` calls `_create_logged_model` unconditionally — there is no flag or env var to disable it. A 3.x pin resolves, installs, trains to completion and *then* dies on a 404 at registration, having burnt the whole job. `azureml-mlflow` only caps (`mlflow-skinny<=3.13.0`) and sets no floor, so taking the newest version it allows is exactly the trap. |
| AML environment | Registered by `register_assets.sh` from the image digest, with **no `conda_file`**. The runtime image is `python:3.11-slim`, which has no conda, so a conda layer cannot build on it. The Azure-side packages (`azureml-mlflow`, ADLS client, inference server) go into the image through the `azure` extra and the `PIP_EXTRAS` build arg — so what deploys is exactly what CI built and Trivy scanned, rather than something AML assembles afterwards. |
| CI identity | One identity, created by `bootstrap.sh` together with its GitHub federation, then read by Terraform as a data source and granted its roles. A second Terraform-created identity would take the grants while CI kept authenticating as the first. The federation stays out of Terraform because an identity able to write its own federated credentials can authorise any branch or environment it likes. |
| Scoring responses | `score.py` returns `AMLResponse`, not a JSON string. A returned string makes Azure ML answer HTTP 200 whatever the body says, which would have made every contract violation look like a success to a caller. |
| Serving entrypoint | The Dockerfile has a separate `azureml` target that resets `ENTRYPOINT` and starts `azmlinfsrv`. AML does *not* override a custom image's entrypoint on an online deployment — it injects `AZUREML_ENTRY_SCRIPT`, mounts the code directory and runs the image as-is — so the CLI entrypoint would exit immediately and every deployment would crash-loop. It is a separate target rather than an edit in place because GCP's Cloud Run Jobs and the local CLI both depend on that entrypoint. |
| Terraform and images | Terraform has no `image_digest` variable. Deployments own the image, and putting the digest in Terraform state would make every release a state change and every `plan` diff-noisy against whatever was last deployed by hand. |
| Secrets | None. CI federates through OIDC, runtime uses user-assigned managed identities, storage has `shared_access_key_enabled = false` and the registry has `admin_enabled = false`. There is no credential in this repository to rotate, expire, or leak. |
| Resource group | One group per environment holds everything, named by `resource_group_name` (dev overrides it to `firmAware`). Terraform state stays in its own `rg-firmaware-tfstate`, because a group that can be destroyed by the root that lives in it is not a safe place for the file describing how to rebuild it. |
| Workspace identity grants | Contributor at each **individual** dependency — Key Vault, workspace storage, ACR, Application Insights — plus the data-plane halves (Key Vault Administrator, Storage Blob Data Contributor). Microsoft's documented table, not a guess. The group-scope grant stays read-only `Reader`; it does not substitute, because provisioning performs control-plane *writes* on all four. |
| Workspace storage data plane | The workspace identity holds **Blob, Table *and* Queue** Data Contributor on the workspace storage account, not Blob alone. Batch endpoints run on ParallelRunStep, which keeps telemetry and heartbeats in Tables and distributes mini-batches through a Queue; `Contributor` grants none of that, being control plane only. With `shared_access_key_enabled = false` there is no key fallback, so each missing role is fatal, and they fail *sequentially* — granting Tables just moves the error from startup to task creation. Training never touches either surface, so this is invisible until a batch job is actually invoked, where it surfaces as exit code 42 with an empty user log. |
| Workspace storage auth mode | `storage_account_access_type = "Identity"`. Azure ML otherwise defaults `systemDatastoresAuthMode` to `accesskey`, which directly contradicts `shared_access_key_enabled = false` on that account: the workspace provisions, then every data-asset registration and job output fails with `KeyBasedAuthenticationNotPermitted`. |
| Batch endpoint identity | `SystemAssigned`, unlike the online endpoint. Batch endpoints reject a user-assigned identity outright. Nothing is lost: a batch endpoint is only a routing name, and the work runs on the training cluster under the workspace identity, so append-only on `scores` is still enforced where the writes happen. |
| Batch config carriage | The model version reaches `batch_score.py` through a `model_version.txt` sidecar in the **code snapshot**, not through `environment_variables`. Batch deployments silently discard that block: the schema advertises it, the CLI accepts it, and the created deployment reads back `environment_variables: {}` — the job definition in `logs/azureml/executionlogs.txt` carries only AML's own `AML_PARAMETER_*` keys. Online deployments *and* command jobs both honour it, which is precisely what makes this look safe. Without the sidecar every published row reads `model_version=unknown`. `deploy_batch.sh` writes the file and removes it again on a trap. |
| Batch deployments are always recreated | `deploy_batch.sh` runs `create` unconditionally, which is an upsert, rather than skipping when the deployment name already exists. The deployment is named for the *model* version, but the image, the scoring script and the environment version all move independently of it — so "it already exists" meant serving stale code, most damagingly on the rollback path, whose entire purpose is to restore old code. |
| Scores are staged, then published | Batch endpoints physically cannot write to `scores`. `--output-path` at that datastore fails `DatastoreTypeNotSupported` — batch rejects `azure_data_lake_gen2`, and the lake must be HNS to be addressable over `abfss://`. Registering the same container again as an `azure_blob` datastore gets past that and scoring succeeds, but the driver's final concat still fails HTTP 409 *"blob is immutable due to a policy"*: `protected_append_writes_all_enabled` permits append-style writes only, and AML uploads whole blobs. So batch stages raw output on the workspace store — scratch space — and `job-publish-scores.yaml` promotes it through the same `write_scores` the `predict` CLI uses. `run_batch_scoring.sh` drives both halves. |
| Publishing runs on the cluster | Not on the CI runner. The append-only role on `scores` belongs to the workspace identity; the CI identity holds `AzureML Data Scientist` and no storage data-plane grant at all, so a runner-side publish could not write there even if the policy allowed it. |
| `AZURE_CLIENT_ID` on cluster jobs | Any job that authenticates to storage from the cluster must set it. The cluster runs a **user-assigned** identity, but `DefaultAzureCredential`'s managed-identity probe asks IMDS for the *system-assigned* one unless `AZURE_CLIENT_ID` names another. The failure is `ClientAuthenticationError`, which reads exactly like a missing role assignment and is not one. `run_batch_scoring.sh` resolves it from the compute at submit time rather than hardcoding it, because the value differs per environment. |
| ADLS writes use create/append/flush | `write_scores` calls `create_file(match_condition=IfMissing)` → `append_data` → `flush_data`, never `upload_data(overwrite=False)`. The latter does not mean what its name suggests on ADLS: it appends to a path it never creates, so a brand-new score object fails `PathNotFound`. It is also the only shape the `scores` container accepts, since protected append writes refuse whole-blob uploads. `IfMissing` sends `If-None-Match: *`, making the create the ADLS counterpart of GCS's `if_generation_match=0`. |
| A dirty tree cannot be tagged with a commit SHA | `build.sh` refuses to build when the image inputs differ from `HEAD`. Its tag is a commit SHA but its build context is the working tree, and the SHA short-circuit that makes the script idempotent then returns whatever was pushed under that SHA earlier — so a rebuild after an uncommitted change silently yields a *stale* image. This cost real time: a rebuild returned a day-old image predating three fixes, and the symptom appeared two deploys later as a model that would not load, which looks nothing like a build problem. CI passes `GITHUB_SHA` explicitly and controls its own checkout, so the guard never fires there. Locally, either commit or set `FIRMAWARE_ALLOW_DIRTY=1`, which tags `dirty-<content-hash>` — honest about not being a commit, and still content-addressed so the digest changes if and only if the image would. |
| The resource group is read, never restated | Both deploy workflows resolve it with `deploy/azure/resource_group.sh`, which parses `envs/<env>.tfvars`. The workflows previously carried `rg-firmaware-dev` as a literal while dev's tfvars said `firmAware`, and the mismatch is silent: `az acr list -g <missing-group>` returns an empty list rather than an error, so the run continued with an empty registry name and failed several steps later pointing at nothing. |
| Every image build names its `--target` | `docker build` with no target builds the **last** stage in the Dockerfile, so appending the `azureml` serving stage retargeted every consumer that relied on the default — the shared CI job started building the inference server and failed that stage's own import guard, and the GCP build would have shipped it to Cloud Run Jobs, where the entrypoint is not the firmaware CLI. Neither build mentions `azureml`, so neither looked like a suspect. |
| checkov suppressions live on the resource | Inline `#checkov:skip=<id>:<reason>` rather than a config-file skip list or `soft_fail: true`. A global list also suppresses resources added later that nobody assessed; an inline skip cannot spread. A test asserts every skip carries a reason of real length, because a bare skip converts a blocking control into no control and reads in a diff exactly like a fix. Of the 27 original failures, four were genuine and are fixed (no NSG on `snet-scoring`, no soft delete on the workspace storage account, `local_user_enabled`, `local_auth_enabled`); the rest are Premium-only ACR features, controls behind `var.network_isolation` that checkov cannot evaluate from module source, or deliberate choices on replication and CMK. |
| Blob logging names its categories | The `audit` category group covers `StorageWrite` and `StorageDelete` but **not** `StorageRead`. On a lake holding both the inputs and the scored outputs, who read the data is the half of the audit trail that matters most, and it was the half that was missing. checkov's own check for this (`CKV2_AZURE_21`) is skipped rather than satisfied: it demands `azurerm_log_analytics_storage_insights`, whose `storage_account_key` argument the provider marks **required** — the one credential `shared_access_key_enabled = false` exists to eliminate. |
| checkov emits `cli` only, never SARIF | The action writes `results.sarif` into the workspace from a container running as **root**, so the Gitleaks step later in the same job cannot overwrite it: gitleaks logs `no leaks found` and then dies on `open results.sarif: permission denied`, which in a failed job reads exactly like a leak was detected. Nothing consumed the SARIF — there is no upload step — so it was only ever debris. This surfaced the first time gitleaks actually ran; checkov had failed ahead of it on every previous attempt. |
| Trivy exceptions are a reviewed file, not a flag | `.trivyignore` carries every suppression with its reachability argument written out, and the step still runs `exit-code: 1` on HIGH and CRITICAL — that gate is the compensating control for a Basic registry in dev, where ACR quarantine is unavailable. Every entry descends from the same two pins: `mlflow==2.22.5` (AML has no `/api/2.0/mlflow/logged-models`, so MLflow 3 404s after training completes) and `azureml-mlflow==1.62.0.post5` (which declares `cryptography<49.0.0` and `mlflow` in turn declares `pyarrow<20`). In each case the fix version is one a pin forbids: adding a floor produces `ResolutionImpossible`, not a patched image. The mlflow CVEs are almost all tracking-server and model-server flaws, and this image only ever uses MLflow as a client. |
| The base image's `setuptools` is deleted, not suppressed | Three findings came from `/usr/local/lib/python3.11/site-packages/setuptools` and its vendored `jaraco.context` and `wheel`. The venv is built without `--system-site-packages` and is first on `PATH`, so nothing in the image can reach any of it. Removing it is a real reduction in attack surface and takes the build toolchain out of a runtime image that has no business compiling anything. |
| `pip` is not shipped in the venv | The last two findings — `msgpack 1.1.2` and `setuptools 70.3.0` — were not installed packages at all. Trivy reported them from `pip/_vendor/bom.cdx.json`, pip's own SBOM of the libraries it vendors, so no version bump could have moved them. A runtime image does not need pip: the AML environments carry no `conda_file`, so AML never resolves packages inside the container, and `mlflow.pyfunc.load_model` defaults to `env_manager="local"`, which restores nothing. Verified by running the serving stage under AML's real contract afterwards — liveness `200`, `/score` returning scores with `model_version` and `threshold`, and a malformed body still answering `422`. |
| The Azure CI suite installs what it imports | The `application` job installed `.[dev]` and then ran the full acceptance suite, which imports `mlflow` and `joblib` from the `train` extra. It could not even collect — six modules failed on `ModuleNotFoundError`, and had never passed. It now installs the same `.[dev,app,train]` the shared CI job does, because it runs the same tests. |
| Two federated subjects per environment, not one | GitHub is migrating the OIDC subject to an immutable form embedding the numeric owner and repository IDs — `repo:owner@<owner_id>/repo@<repo_id>:environment:<env>` — so a rename cannot silently redirect an existing trust. The rollout is not driven by this repository's settings: `GET /repos/{o}/{r}/actions/oidc/customization/sub` reported `use_immutable_subject: false` while the issued token already carried the new prefix. Entra then answers **AADSTS700213**, naming the subject it was offered but never the one it holds, so it reads like a workflow bug. `bootstrap.sh` registers both subjects; a federated credential is an exact string match, so an unused one costs nothing and an absent one costs a deploy. A test asserts every registered subject is still `:environment:`-scoped — the point of the gate is that no branch-subject door exists. |
| `tfsec` is pinned and authenticated | The action defaults to resolving `latest` through an **unauthenticated** GitHub API call on every run. That fails outright when the runner's shared IP is rate limited — a red security job caused by nothing in the repository — and it leaves the scanner version floating, so the same Terraform can pass one day and fail the next. Pinned to `v1.28.14`, tfsec's final release before the project was folded into Trivy, and given `secrets.GITHUB_TOKEN`. Same reasoning as the exact `ruff` pin in `pyproject.toml`. |
| Terraform federates on its own, in the job's `env` | `Azure/login` authenticates the **az CLI**, and the azurerm provider refuses to reuse a CLI session belonging to a service principal: `Error building ARM Config: Authenticating using the Azure CLI is only supported as a User (not a Service Principal)`. `ARM_USE_OIDC` plus `ARM_CLIENT_ID` / `ARM_TENANT_ID` / `ARM_SUBSCRIPTION_ID` make it request its own token from the Actions endpoint, covering both the provider and the `azurerm` state backend (`use_azuread_auth = true`). They sit on the **job**, not the workflow: environment-scoped variables only resolve once a job declares its `environment`, and at workflow scope `vars.AZURE_CLIENT_ID` would silently read the unset repository-level variable — which is an empty string, not an error. |
| CI holds the append-only role on `scores`, not read | The batch smoke test reads the score object the run just published, then asserts a **second write to it is refused**. `Contributor` on the resource group is control plane only — it lets CI manage the storage account and grants no access at all to the data inside it — so the step failed on `You do not have the required permissions` without ever reading a byte. The az CLI suggests `--auth-mode key`; that is not available here and should not be, because `shared_access_key_enabled = false` is deliberate and several checkov skips rest on it. The fix is a data-plane grant, and *which* role matters: `Storage Blob Data Reader` would be enough to read the object back and would make the immutability assertion **meaningless**, since the overwrite would be refused for lack of permission rather than by the policy — the test would report the container is immutable without ever testing it. `Storage Blob Data Contributor` would grant delete on the one container whose whole purpose is that nothing can remove evidence, to the identity most exposed to a leaked token. The custom appender role is exactly the middle: CI could have succeeded, so the refusal means something, and it still cannot delete. |
| CI needs data-plane write on the **workspace** account too | `az ml batch-endpoint invoke --input <local file>` does not hand the path to the service. The CLI uploads the file to the workspace's default datastore (`workspaceblobstore`) and passes a URI, and a job spec's `code:` directory is uploaded the same way — so the **caller** needs data-plane write on the workspace storage account. Without it the invoke fails at `You don't have permission to alter this storage account` *after* the deployment has been resolved and its settings printed, which reads like an endpoint fault rather than a caller-credential one. This is AML's working storage — run history, snapshots, job staging — and it is a different account from the lake precisely so evidence is not mixed in with scratch, so `Storage Blob Data Contributor` here reaches nothing the append-only rule protects. Account-scoped for the same reason as the workspace's own grant: AML creates and names those containers itself. |
| pip stays in the runtime venv | It was removed once, and it was the right-looking call: pip is a build-time tool, and it is the last source of trivy findings — it vendors `msgpack` and `setuptools` under `pip/_vendor` and ships `bom.cdx.json` describing them, which trivy reads as installed packages even though the real installs are already patched (1.2.1, 84.0.0). Removal cleared the scan, the CLI ran, and the serving stage answered `/score` under AML's real contract, because `azmlinfsrv` never shells out to pip. **The batch driver does.** `driver/amlbi_main.py` runs `python -m pip` while initialising, and without it the job dies at `No module named pip` with exit 42 *before the scoring script is called* — a batch failure with an almost empty user log and no mention of the deployment. Nothing available before a live batch run could have caught it: the container tests, the image scan and the serving rehearsal all passed. The two findings are suppressed in `.trivyignore` instead, which is also the only place `ignore-unfixed` cannot help, because trivy believes both to be real installs with real fixes. |
| The security gate logs in again first, and reports only what it observed | Two defects surfaced together the first time the gate ever ran. The client assertion `Azure/login` exchanges is valid for **five minutes**; batch scoring takes closer to ten because the cluster scales from zero, so every token the CLI had not already cached failed `AADSTS700024`. Underneath that, control 6 had never worked at all: `az monitor diagnostic-settings list` returns a plain array, not a wrapper object, so `--query "length(value)"` evaluated `length(null)` and errored on every call. `\|\| echo 0` turned that into "no diagnostic setting" and reported four settings that all existed as missing. The fix is a second login before the gate, plus queries that surface an error instead of substituting a value. The same `\|\| true` pattern in control 4 was the **more dangerous** direction — an identity whose role assignments could not be read was reported clean — so that now reads by `--assignee-object-id`, which needs no Microsoft Graph permission, the one thing the deploy identity deliberately does not have. A gate that cannot tell "I checked and it is missing" from "I could not check" is not a gate. |
| The hosted page is **public**, deliberately | The hosting spec asks for Entra ID sign-in with `RedirectToLoginPage`. The owner overrode it: *"Anyone with the link, no login at all — fully public."* This is a decision, not an omission, so it is written down and it is **tested** — `smoke_test_app.sh` control [2] fails the deploy if `GET /` ever answers 302, 401 or 403, which forces a future change of access model to update this row and that check together instead of one silently drifting from the other. The compensating control is what the page's identity can do rather than who can reach it: three `Storage Blob Data Reader` grants and `AcrPull`, and nothing else. It cannot append to `scores`, cannot read the vault, and holds no Contributor anywhere — so the worst an anonymous visitor can reach is the published scores, which are the artefact the page exists to show. Control [7] asserts that set is *exactly* four assignments, so a later grant cannot quietly widen it. |
| The page is a container, not an App Service | It is the same repository, the same `Dockerfile` and the same digest-pinned release path as the model image — a fifth stage (`app`) rather than a second deployment mechanism. That keeps one build to scan, one promotion rule, and one thing to reason about when asking what is running. Container Apps also scales to zero, which matters because the page is idle most of the time. |
| The page reads the scores container **root** | The spec's `${scores_uri}/scores` is wrong against this environment: the batch pipeline publishes `scores_<timestamp>_<jobid>.csv` at the container root, so a prefix underneath it lists nothing. That failure is silent and it is the dangerous kind — `app.py` falls back to the committed demo fixture, so the page renders plausible numbers that came out of the repository rather than out of the pipeline. Verified against live storage before wiring, and asserted in the acceptance tests. |
| Terraform owns the container app, a deploy owns the revision | The same infrastructure-versus-release split as the batch endpoints. `lifecycle.ignore_changes` covers the container image and the ingress traffic map, because without it the next `terraform apply` would revert a promotion, or roll the image back to whatever digest the previous apply happened to see. |
| `TF_VAR_app_image` is now required to plan | `app_image` has no safe default: the module rejects anything that is not `@sha256:…`, because a tag can move between the plan and the apply. The value only matters on create — changes are ignored — but a plan still has to name one. A local `terraform plan -detailed-exitcode` drift check therefore needs `TF_VAR_app_image=$(az containerapp show -n ca-firmaware-<env> -g <rg> --query "properties.template.containers[0].image" -o tsv)` in front of it, otherwise it fails the precondition rather than reporting drift. |
| Terraform's `latestRevision: true` had to be pinned before every deploy | On the very first release there is no revision to name, so Terraform creates the app with a traffic map of `latestRevision: true`. Left that way, `az containerapp update` hands the new revision **100% of traffic the instant it exists** — before anything has tested it, which is precisely the design this pipeline exists to avoid. `deploy_app.sh` therefore resolves the live revision *by name* and pins traffic to it before creating the new one, then re-reads the traffic map and fails if any of it moved. |
| The page subnet is a `/23`, not the `/27` minimum | Prod only; dev runs `network_isolation = false` and has no VNet at all, so the app takes a Consumption-only environment on platform-managed networking. Where there is a VNet, Container Apps holds addresses for superseded revisions during a rollout, and the environment's subnet **cannot be resized afterwards** — changing it means recreating the environment, which changes the public FQDN. `/23` is chosen once, at 10.42.8.0/23, past the three existing `/24`s. The delegation to `Microsoft.App/environments` and the workload-profile block switch together, because workload profiles require a delegated subnet and Consumption-only rejects one. |
| Adding the page pushed the batch check past the assertion window | The five-minute `AADSTS700024` problem the security gate already solves, reached from a new direction. The token minted at login covers ARM, so registering assets, resolving the model and pointing the endpoint all kept working from cache; the batch smoke test is the first step to ask for a **storage** audience, and acquiring a new audience needs the assertion rather than the cache. The page steps sit at the top of the job — deliberately, so a page regression reports in about three minutes instead of twenty — and the ten minutes they add is what moved that request outside the window. Worth stating plainly because it is a property of *ordering*, not of either feature: inserting any slow step ahead of a first-use-of-an-audience step can reproduce it, and the failure names a credential rather than the thing that moved. Fixed with a refresh immediately before the consumer, which is where the existing one sits too. |
| An unreadable control fails; an absent one does not | `length(properties.configuration.secrets)` errored on exactly the apps that should pass it, because an app with no secrets reports `"secrets": null` and JMESPath `length(null)` raises. The tempting fix is `\|\| echo 0`, which is the same laundering of an error into a passing value that control 6 of the security gate was built to remove. The distinction the gate actually needs is between *absent* and *unreadable*: the object is read as JSON, a non-zero exit or empty output still fails the deploy, and only a null or missing key counts as the genuine zero it is. The check was strengthened while open, to assert the positive form as well — the registry must pull with an identity and reference no password secret, since an empty secret list sitting beside a password reference would otherwise pass. |
| The page's context comes from the scoring **input**, and nothing in CI writes it | A score row carries `deployment_id`, a probability, a band and a model run — nothing that says what the device *is*. Every panel describing equipment (Equipment Record, Cross-Vendor & Vulnerability Context, Risk Flag Panel, and the vendor and deployment-type breakdowns) is produced by joining scored rows back onto `data/upcoming_deployments.csv`. Three consequences, and all three bit. The file is seeded by a human at bootstrap, because CI holds no data-plane write on `data` and deliberately should not — so a fresh environment has scores and no context until someone lands it. The run that produces the scores has to have scored *that* file, and the Operating section used to point at the five-row smoke fixture instead, so following the documentation reproduced the fault. And because the batch smoke test publishes into the same append-only container as a real run while the page opens on the newest one, dev's newest run is usually those five synthetic rows — so seeding only the real input leaves the page looking broken again after every deploy, which is worse than not fixing it, because it looks fixed. The fixture is therefore seeded alongside the real input; the schemas are identical and `UPC_*` never collides with `smoke-*`. The failure is quiet by construction — `app.py` degrades to a sidebar warning rather than stopping, which is right for an operator page and unhelpful for whoever wired it. |
| The page reads the threshold from the run, not from the champion | `load_champion` reads the `artifacts` container, and on Azure **nothing writes it**: `publish_artifact_run` refuses ADLS outright, because the model registry versions the run there and a champion pointer would be a second source of truth. So the lookup failed on every load and the 0.5 default stood in against a real threshold of 0.18 — and that number is not decoration, it sets the gauge zones, so the needle could sit in green on a row labelled HIGH beside it. The value was already in hand: the publish step records `threshold` and `model_version` on every scored row, so the page had loaded the answer and was reaching past it to a source that could not reply. Reading the run's own figure is also *more* correct than the champion would have been — selecting an earlier run is exactly when the two differ, and the threshold that explains a band is the one that row was scored with. The pointer remains the source where scores carry no run facts, which is every local and GCS run, since `predict.py` writes only `SCORE_COLUMNS` plus `scored_at`. |

## Resource topology

Each environment resource group contains:

- **Foundation** — Log Analytics workspace, Application Insights, Container
  Registry (`admin_enabled = false`), Key Vault (RBAC authorization, soft
  delete), and diagnostic settings routing every resource's logs and metrics to
  Log Analytics with 90-day retention. Application Insights lives here rather
  than beside the workspace that consumes it because the workspace identity must
  hold Contributor on it *before* the workspace exists; declared in the workspace
  module, that grant is unexpressible.
- **Storage** — two accounts. The **lake** is ADLS Gen2 with hierarchical
  namespace and four containers (`data`, `artifacts`, `scores`, `collected`),
  addressed as `abfss://`. The **workspace account** (`stw…`) is plain, because
  Azure ML rejects an HNS account as a workspace's system storage ("Cannot use
  storage with HNS enabled"); it holds run history and snapshots only. Terraform
  state lives in a third account created by `bootstrap.sh`, so a mistake in this
  root cannot destroy the state that describes it. Lifecycle management tiers
  artifacts to cool; `scores` has an immutability policy and no expiration rule.
- **Identity** — three user-assigned managed identities (workspace, endpoint,
  CI/CD) with role assignments scoped to individual containers rather than to
  the account, each carrying a comment naming why that principal needs that role
  on that scope.
- **Workspace** — Azure ML workspace, a compute cluster that scales to zero
  between jobs, and a datastore bound to the account by identity rather than key.
- **Endpoints** — a batch endpoint with its deployment, and an online endpoint
  with `blue`/`green` slots (created only when `online_endpoint_enabled`).
- **Monitoring** — action group, budget with threshold alerts, and alert rules
  for job failure and endpoint error rate.
- **App** — a Container Apps environment and a single container app serving
  `app.py` on 8501 behind managed HTTPS, running the `app` stage of the same
  Dockerfile under a fourth user-assigned identity that can only *read*. In dev
  it scales to zero; in prod it holds one replica and sits on the delegated
  `snet-apps` subnet.
- **Policy** — four built-in policy assignments at resource-group scope.

## Threat model

The question this section answers is: **an attacker holds a valid GitHub token
for this repository. What can they reach?**

They can open a pull request. They cannot merge one — `main` requires review —
and a pull request from a fork receives no Azure credential at all. Every
federated credential's subject is
`repo:PMK1991/FirmAware:environment:{dev|production}`, and a workflow only
produces that subject when the job declares that GitHub environment, which a
fork cannot cause. CI on a pull request therefore runs lint, tests, Trivy,
checkov and gitleaks with no cloud access whatsoever — `azure-ci.yaml` does not
even request `id-token`, so it cannot authenticate to Azure by construction.

If they can push to `main`, the dev deploy workflow runs and its `dev`
environment mints a token for the dev CI identity. That identity is
`Contributor` and `User Access Administrator` **on the dev resource group
only**. It is the widest grant in the system and it is deliberately bounded:
Terraform must be able to create resources and assign roles to the identities it
creates, but at resource-group scope re-granting can only reach an environment
the principal already controls. At subscription scope the same pair would be a
privilege-escalation path to every other resource group in the tenant. So the
blast radius of a compromised `main` is: the dev environment, entirely.

They cannot reach prod from there. The prod identity has **only** an environment
credential, subject `…:environment:production`, and no branch credential at all
— that omission is the control. A token is minted only after the
required-reviewer gate on the `production` environment passes, so the approval is
a precondition of authentication rather than a UI convention layered on top of
the deployment. And `azure-deploy-prod.yaml` refuses any digest that is not
currently serving in dev, so even an approved run cannot ship an artifact that
was never smoke tested.

What they cannot do in **either** environment:

- **Exfiltrate data with a stolen connection string.** There is none.
  `shared_access_key_enabled = false` means the account has no key to steal, and
  every access path is an AAD token bound to a managed identity.
- **Delete score evidence.** The runtime's role omits `blobs/delete`, and the
  container carries a time-based immutability policy. In prod that policy is
  locked, so neither a blob, the container, nor the account can be removed until
  retention expires — deleting the resource group does not get around it. In dev
  the policy is unlocked and set to one day, precisely so the environment stays
  disposable; the append-only RBAC is identical in both.
- **Backdoor the serving image.** The workspace and endpoint identities hold
  `AcrPull`, not `AcrPush`. Nothing that runs a job can change what a later job
  executes.
- **Silently weaken a control.** The four Deny/Audit policies sit at
  resource-group scope and evaluate every request regardless of who makes it;
  reverting a storage setting in Terraform does not disarm them.
- **Move traffic without a smoke test.** `promote_traffic.sh` re-probes through
  the endpoint after each 10/50/100 step and reverts on the first failure.

The residual risks, stated plainly: a compromised `main` can destroy the dev
environment, and a malicious approver can ship a digest that dev genuinely was
serving. Neither is closed by anything in this repository — the first is bounded
by scope, the second by requiring a second human.

## Compliance mapping

| Assignment (`{prefix}` = `firmaware-{env}`) | Built-in definition GUID | Effect | Control |
|---|---|---|---|
| `{prefix}-no-public-blob` | `4fa4b6c0-31ca-4c0d-b10d-24b96f62a751` | Deny | CIS Azure 3.6, ISO 27001 A.9.4 — no anonymous container access |
| `{prefix}-tls12` | `fe83a0eb-a853-422d-aac2-1bffd182c5d0` | Audit | CIS Azure 3.1, ISO 27001 A.10.1 — TLS 1.2 transport floor |
| `{prefix}-no-acr-admin` | `dc921057-6b28-4fbe-9b83-f7bec05db6c2` | Deny | CIS Azure 5.x, ISO 27001 A.9.2 — no shared registry credential |
| `{prefix}-tag-{app,env,owner,cost_center,managed_by,data_classification}` | `871b6d14-10aa-478d-b590-94f262ecfa99` | Deny | ISO 27001 A.8.1 — asset ownership and attribution |

The tag policy is assigned six times rather than once because the built-in
definition takes a single tag name. Six assignments report *which* tag is
missing; one would only report that something was.

Controls enforced in Terraform rather than by policy, because no built-in
definition expresses them: append-only score RBAC (ISO 27001 A.12.4, audit log
protection), container immutability (A.12.4 / A.18.1, retention), and OIDC-only
CI authentication (A.9.2, no static credentials).

## Rollback

| Class | Mechanism | Target | Rehearsed |
|---|---|---|---|
| Traffic | `rollback_endpoint.sh` flips the traffic map back to the retained slot | < 30 s | *pending live apply* |
| Page | `rollback_app.sh` flips the container app's traffic map back to the retained revision, which is still provisioned at 0% | < 60 s | **fired in anger, 23 s** — see below |
| Model | `rollback_model.sh` redeploys the prior registered version into the idle slot | < 15 min | *pending live apply* |
| Image | Re-run prod promotion with the previous digest | < 15 min | *pending live apply* |
| Infra | `git revert` → CI re-applies; state protected by blob versioning and lease locking | < 30 min | *pending live apply* |
| Data | Restore the prior blob version, re-run `validate` | < 10 min | *pending live apply* |

Scores are **non-rollbackable by design** — superseded, never deleted. That is
enforced by the platform (append-only role plus immutability policy), not by
convention, so it holds even against an operator who wants to undo one.

Rehearsal times are recorded here after the first live apply. They are
deliberately left as `pending` rather than filled in with the design targets,
because an unrehearsed number in this column would be a claim the environment
has not earned.

The page rollback is the one row that has run for real, and it is worth reading
as a warning rather than as a credential. A failed smoke test triggered it on
the first release. It completed in 23 seconds, reported success — and had
flipped 100% of traffic onto the very revision whose smoke test had just
failed. Terraform creates the app routing to `latestRevision` rather than to a
named revision, because on a first release there is nothing to name; the script
read that unnamed entry as "nothing is live", and its fallback then chose the
most recently created revision, which is exactly the one it should have been
running away from.

Two things follow. The first is fixed in code: the flag is resolved to the
revision it denotes, so the exclusion has something to exclude, and a first
deploy now correctly reports that there is no rollback target and fails rather
than inventing one. The second is a caveat on this table — the mechanism has
still never been rehearsed against a genuine previous revision, because no
release on this branch has yet had one. It stays *fired in anger* rather than
*rehearsed* until it has.

## Cost

Dev, with `online_endpoint_enabled = false`:

| Item | Monthly |
|---|---|
| Storage (ADLS Gen2, < 10 GB, LRS) | ~$1 |
| Container Registry, Basic | ~$5 |
| Log Analytics (90-day retention, low volume) | ~$5 |
| Key Vault (RBAC, few operations) | ~$1 |
| Compute cluster, scales to zero, ~10 h/month of `Standard_DS3_v2` | ~$8 |
| Container app, `min_replicas = 0`, 1 vCPU / 2 GiB while awake | ~$0–3 |
| Managed online endpoint | $0 — disabled |
| **Total** | **~$20/month**, against the spec's $40 ceiling |

The budget in `dev.tfvars` is set to $40 with alerts, so the ceiling is
monitored rather than assumed.

Enabling the online endpoint in dev adds roughly **$70/month** for a single
`Standard_DS2_v2` instance. Managed online endpoints cannot scale to zero: the
instance bills continuously whether or not it is called. That single fact is
what makes `online_endpoint_enabled = false` the dev default, and it is why
`azure-deploy-dev.yaml` branches on the Terraform output rather than assuming an
endpoint exists.

Prod, with `min_instances = 1` on `Standard_DS3_v2`, five private endpoints and
a Premium registry, lands near **$300/month** — which is the budget set in
`prod.tfvars`.

## Where dev deviates

Three controls from the implementation spec are relaxed in dev. Each is a named
variable rather than a silent default, each is scoped to `envs/dev.tfvars`, and
`envs/prod.tfvars` leaves all three at their secure defaults.

**RELAXED 1 — `network_isolation = false`.** No private endpoints, no managed
VNet isolation. Five private endpoints at ~$7.30 each, plus the Premium registry
they require, cost more per month than the whole of the rest of dev.
*What is lost:* traffic to storage, the vault and the registry traverses the
Azure backbone over public endpoints instead of private IPs.
*What is not lost:* authentication is unchanged. AAD is still the only way in,
because shared keys are disabled — this relaxation removes a network boundary,
not an identity one.
*Direct consequence, spelled out in `dev.tfvars` rather than left implicit:*
`storage_network_default_action = "Allow"`. With no private endpoints, the
compute cluster and CI reach storage over its public path, so a default-deny
firewall would lock dev out of its own data. Prod never evaluates this line —
isolation forces `Deny` regardless of the variable.

**RELAXED 2 — `registry_sku = "Basic"`.** Private link, immutable tags,
quarantine and retention policies are all Premium-only features.
*Compensating control:* the blocking Trivy scan in `azure-ci.yaml`, which refuses
to promote an image carrying a HIGH or CRITICAL finding. Digest-pinned
deployments also make the missing immutable-tag feature largely moot: nothing in
this system resolves an image by tag.

**RELAXED 3 — `key_vault_purge_protection = false`.** A purge-protected vault
reserves its name for 90 days after deletion, which makes a throwaway
environment impossible to tear down and rebuild.
*What is retained:* soft delete stays on, so an accidental delete is still
recoverable within the retention window; only the irreversible-by-design
guarantee is off.

Additionally `scores_immutability_locked = false` and
`scores_retention_days = 1` in dev. The immutability policy itself is still
created and the append-only role assignment is byte-identical to prod — only the
lock comes off, so dev can still be destroyed. The negative test that proves the
runtime cannot delete a score works identically in both environments.

Two cost settings are also dev-only and are not security relaxations:
`online_endpoint_enabled = false`, because a managed online endpoint cannot scale
to zero and bills ~$70/month idle; and `online_min_instances = 0`, so that if the
endpoint is switched on for a test it holds no warm capacity. Prod sets `1` — the
deploy script's own fallback is `0`, and a prod endpoint with no warm instance
turns the first request after a quiet period into a multi-minute cold start.

## What does not relax, in either environment

- `shared_access_key_enabled = false` — no account key exists to leak
- `admin_enabled = false` — no registry password exists to leak
- append-only RBAC on `scores` — the runtime cannot delete evidence
- an immutability policy on `scores` — nor can anyone else, for the window
- no service-principal passwords — CI federates, it does not authenticate
- OIDC subject scoped to a single GitHub environment per Azure environment
- diagnostic settings on every resource, 90-day retention
- the six mandatory tags, enforced by policy

## Deliberate deviations from the implementation spec

Four places where this implementation does something other than what
`docs/design/azure-implementation-spec.md` literally says, each because the
literal reading would be less safe or would not work.

**§5 says every workflow gets `permissions: id-token: write, contents: read`.**
`azure-ci.yaml` gets `contents: read` only. It runs on `pull_request`, which
means it executes code from forks, and it needs no Azure access to run ruff,
pytest, tflint, checkov or Trivy. Granting `id-token` there would create an
authentication path that has no use, on the one workflow most exposed to
untrusted input. The spec's "and nothing more" is the intent; this is stricter.

**§5 says `terraform apply … -var image_digest=…`.** There is no `image_digest`
variable. Deployments own the image, not Terraform: putting a per-release digest
into Terraform state makes every release a state change and makes every `plan`
diff against whatever was last deployed. The digest is instead passed to
`deploy_endpoint.sh`, which stamps it onto the deployment as a tag — which is
also what makes the prod provenance check possible.

**§5 says prod triggers on tag `v*`; §7's rollback story needs a manual path.**
Both exist. On a tag, the digest is *read from* the dev endpoint, so nothing is
promoted that dev was not serving. On `workflow_dispatch` the digest is
*supplied* and then checked against dev, which is what makes a targeted
roll-forward to a specific earlier version possible. Both paths converge on a
digest that dev provably served, and the deploy job only ever reads the resolved
output, never the raw input.

**§2.4 lists the CI identity's roles and `Contributor` is not among them.** It
holds `Contributor` and `User Access Administrator`, both scoped to the single
environment resource group. The spec's list covers what CI needs to deploy an
*application*; this pipeline also owns its infrastructure, and `terraform apply`
cannot create a storage account or a workspace without `Contributor`, nor create
role assignments without `User Access Administrator`. The narrower alternative
— a human applying infrastructure and CI deploying only into it — was rejected
because it puts the resource topology outside the review trail the spec asks for
in §8.

Both grants are constrained rather than accepted as-is:

- Resource-group scope, never subscription. A leaked GitHub token reaches one
  environment and stops at its boundary.
- The `User Access Administrator` assignment carries an ABAC condition denying
  `Owner` and `User Access Administrator` themselves, so CI cannot escalate its
  own privileges. The operator matters: `ForAllOfAllValues:GuidNotEquals` is the
  deny-list form. `ForAnyOfAnyValues:GuidNotEquals` reads plausibly and is
  always true once the list holds two different GUIDs.
- `security_check.sh` control 4 fails the build on any subscription-scope or
  `Owner` assignment found on a runtime identity, so a drift back toward the
  broader posture is caught rather than assumed absent.

## Identities: why there is one CI identity, not two

`bootstrap.sh` creates the identity CI authenticates as, its GitHub federation,
and its access to state. Terraform reads that identity with a data source and
grants it everything else.

This ordering is forced. Terraform cannot create the credential that
authenticates the apply which creates it. Having Terraform create a *second*
identity is worse than redundant — CI authenticates as the bootstrap one, so
every role granted to the second lands on a principal that never presents itself,
and every deploy fails on authorization while the plan looks perfect.

The federation stays with bootstrap for a second reason. For Terraform to manage
it, the CI identity would need write access to its own federated credentials —
and a pipeline that can add a subject to its own identity can authorise any
branch or environment it likes, which is exactly the gate the credential exists
to enforce. Bootstrap runs as a human, so that trust decision is made by someone
the directory already trusts. The cost is that the subject is set by a script
rather than shown in a plan diff, so
`tests/test_azure_deployment.py` asserts it instead: exactly one credential,
always an `:environment:` subject, never a branch one, and matching the
`environment:` each workflow declares.

Bootstrap grants two roles and no more. `Storage Blob Data Contributor` on the
state container is pure `dataActions`, which is why the second grant is needed:
`Reader` on the identity resource itself. Terraform's data source is a
control-plane GET, so without it the very first CI `plan` fails with
`AuthorizationFailed` before evaluating anything. `Reader`, not `Managed Identity
Contributor` — read is all the data source needs, and write would reopen the
self-federation path above.

The consequence is deliberate: **the first apply for an environment is run by a
human.** CI can authenticate as soon as bootstrap finishes, but it holds nothing
beyond state until that apply grants it. From then on no human is in the path.

## Bootstrap

`bootstrap.sh` is idempotent and creates only what Terraform cannot create for
itself: the state account Terraform stores its state in, and the identity CI
authenticates as before any Terraform has run.

```bash
# 1. Sign in with a scope that permits role assignment and app registration.
az login --scope https://management.core.windows.net//.default
az account set --subscription a4b87216-b285-44f7-a7f3-54506c8ffcfb

# 2. State account, bootstrap identity, backend config.
bash infra/azure/bootstrap.sh dev

# 3. Commit the backend config -- CI reads it to find remote state.
git add infra/azure/envs/dev.backend.hcl

# 4. Terraform. This first apply must be a human: it creates the federated
#    credential CI needs, so CI cannot yet authenticate to run it.
cd infra/azure
terraform init -backend-config=envs/dev.backend.hcl
terraform plan  -var-file=envs/dev.tfvars
terraform apply -var-file=envs/dev.tfvars
```

`bootstrap.sh` writes `envs/{env}.backend.hcl`, and **it is committed**. Both
deploy workflows run `terraform init -backend-config=envs/{env}.backend.hcl`, so
ignoring it would leave CI unable to locate its own state. It holds resource
names, not secrets; authentication is the OIDC token the workflow already has.

Also create a GitHub environment named `dev` (and `production` for prod), and set
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` as **that
environment's** variables — not repository variables. Bootstrap creates a
separate identity per environment, so a repository-scoped `AZURE_CLIENT_ID` means
bootstrapping the second environment silently overwrites the first, and the OIDC
exchange then fails for whichever one lost. None of the three is a secret.

The OIDC subject is scoped to the environment, so a workflow that does not
declare it cannot obtain a token at all — which is what gives prod's
required-reviewer gate teeth rather than making it a UI convention.

Set them as GitHub **variables**, not secrets: none is confidential, and marking
them secret only makes CI logs harder to read.

```bash
gh variable set AZURE_CLIENT_ID       --env dev --body "$(az identity show -n id-firmaware-bootstrap-dev -g rg-firmaware-tfstate --query clientId -o tsv)"
gh variable set AZURE_TENANT_ID       --env dev --body "$(az account show --query tenantId -o tsv)"
gh variable set AZURE_SUBSCRIPTION_ID --env dev --body "$(az account show --query id -o tsv)"
```

Skipping this step is not a quiet failure but it is a confusing one. `Azure/login`
reports *"Not all values are present. Ensure 'client-id' and 'tenant-id' are
supplied"*, which reads like a malformed workflow; the workflow is fine, the
variables simply resolve to empty strings because an unset `vars.*` is empty
rather than an error. Setting them at **repository** scope instead makes the same
error appear only in whichever environment was bootstrapped second.

### If the first apply fails on a Key Vault permission

Workspace creation can fail with:

```
User assigned identity doesn't have enough permissions ... does not have
authorization to perform action 'Microsoft.KeyVault/vaults/read' ...
If access was recently granted, please refresh your credentials.
```

Two things make this far more confusing than it reads, and both cost real time
during the dev bring-up:

1. **It names one action however many are missing.** It is not a checklist. The
   full set Azure ML requires is in the Decisions table above and is now created
   by the identity module, so a fresh environment should not hit this at all.
2. **Azure ML caches the failure against the workspace *name*.** Once creation
   has failed, retrying the *same name* keeps returning the same error for tens
   of minutes after the permissions are correct — while an otherwise identical
   request under a new name succeeds immediately. So a retry that still fails is
   not evidence that the permissions are still wrong.

If you meet it: confirm the grants exist with
`az role assignment list --assignee <workspace identity principalId> --all`, then
**wait and re-run the same apply**. Do not start granting broader roles to make it
move — subscription-level Contributor does not fix it, because the problem is not
the grant. The `time_sleep` in `main.tf` exists to keep the first attempt from
failing in the first place, which is cheaper than waiting out the cache.

## Operating

```bash
# Seed the lake. The data assets are pointers, so registration succeeds against
# an empty container and training is what fails, several minutes later, inside a
# job. Terraform creates the containers; it deliberately does not upload data.
#
# Subscription Owner does not grant this: the data plane is a separate surface,
# so the upload returns AuthorizationPermissionMismatch until the human running
# it also holds Storage Blob Data Contributor on the account. Grants take a
# minute or two to propagate.
az storage blob upload \
  --account-name "$(terraform -chdir=infra/azure output -raw storage_account_name)" \
  --container-name data --name deployment_events.csv \
  --file data/deployment_events.csv --auth-mode login

# The scoring input, and the page's only source of equipment context. Scores
# carry a deployment_id and nothing else describing the device, so the Equipment
# Record, Cross-Vendor Context and Risk Flag Panel are all produced by joining
# the scored rows back onto this file. Miss it and the page still renders --
# predictions and gauges appear, every panel that needs context comes up empty,
# and the only trace is a sidebar warning. It is seeded here rather than by CI
# because CI holds no data-plane write on `data`, which is the same reason
# `deployment_events.csv` is seeded here.
#
# The smoke fixture goes in with it, and that is not tidiness. The batch smoke
# test publishes into the same append-only scores container as a real run, the
# page opens on the newest run, and in dev the newest run is therefore usually
# the smoke test's five synthetic rows -- whose ids appear nowhere in the real
# input. Seeding only the real file leaves the page looking broken again after
# every deploy, which is worse than never having fixed it, because it looks
# fixed. The two schemas are identical and the id spaces do not overlap
# (`UPC_*` against `smoke-*`), so the join simply finds whichever it is given.
# Prod has no smoke step and so never carries these rows.
{ cat data/upcoming_deployments.csv
  tail -n +2 deploy/azure/fixtures/upcoming_smoke.csv
} > /tmp/upcoming_seed.csv

az storage blob upload \
  --account-name "$(terraform -chdir=infra/azure output -raw storage_account_name)" \
  --container-name data --name upcoming_deployments.csv \
  --file /tmp/upcoming_seed.csv --auth-mode login

# Build and push, emitting a digest.
AZURE_ACR_NAME=$(terraform -chdir=infra/azure output -raw container_registry_name) \
  bash deploy/azure/build.sh dev

# Register the AML environment (pins the image digest) and the data assets the
# pipeline resolves. Must run before training or deployment: every component
# references azureml:firmaware-env@latest.
AZURE_ACR_NAME=... AZURE_STORAGE_ACCOUNT=... FIRMAWARE_IMAGE_DIGEST=... \
  bash deploy/azure/register_assets.sh dev

# Submit training; prints the registered model version on success.
bash deploy/azure/run_training.sh dev

# Deploy into the idle slot at 0% traffic, then prove it works, then promote.
bash deploy/azure/deploy_endpoint.sh dev "$MODEL_VERSION" "$IMAGE_DIGEST"
bash deploy/azure/smoke_test.sh dev green
bash deploy/azure/promote_traffic.sh dev green blue

# Bulk scoring. Two jobs, not one: the batch endpoint stages its output on the
# workspace store because it cannot write to the immutable scores container,
# and a short cluster job then publishes it there. Invoking the endpoint
# directly scores correctly but produces no evidence -- the run still succeeds.
#
# Score the seeded input, not the smoke fixture. Both publish real scores and
# both look correct from the pipeline's side, but the page joins scored rows
# back onto `data/upcoming_deployments.csv` for equipment context, and the
# fixture's ids (`smoke-001`...) appear nowhere in it. Scoring the fixture
# therefore leaves the page rendering predictions with every context panel
# blank -- which is exactly what it did until this line named the right file.
bash deploy/azure/deploy_batch.sh dev "$FIRMAWARE_MODEL_VERSION"
bash deploy/azure/run_batch_scoring.sh dev data/upcoming_deployments.csv

# Or drive the same path and assert on the published object, including that
# overwriting it is refused. This is a *test*, not a way to feed the page: it
# scores five synthetic rows on purpose.
bash deploy/azure/batch_smoke_test.sh dev

# Verify every control. Env-aware: announces dev relaxations, fails on breaks.
bash deploy/azure/security_check.sh dev
```

`security_check.sh` distinguishes a *relaxation* from a *failure*. In dev it
prints what is deliberately off and why, and still fails the run if anything that
should be on is not. A check that silently skipped in dev would prove nothing in
the environment where it is cheapest to catch a mistake.
