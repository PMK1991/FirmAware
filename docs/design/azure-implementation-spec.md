# Spec — FirmAware on Azure: IaC, Scripts, CI/CD

> Implementation specification for a coding agent. Builds the architecture in `docs/design/azure-architecture.md` (v2.1, ML layer only). Self‑contained; where silent, choose the simplest secure option and record it in `infra/azure/README.md`.
> Version 1.0 · 2026‑08‑01 · **Do not modify `src/firmaware/`** except the two additions named in §3.1 — the application is cloud‑agnostic and must stay that way.
> Existing GCP deployment (`infra/`, `deploy/`, `.github/workflows/`) stays untouched and working. Azure lands beside it, never replacing it.

---

## 0. Non‑negotiables (verify these before writing any resource)

Security and compliance are constraints on the first commit, not a later hardening pass. Every one of these is an acceptance test in §8.

1. **No secrets in the repo, ever.** No service‑principal passwords, no connection strings, no `.env`. CI authenticates with **OIDC federated credentials**; runtime uses **managed identity**. `az ad sp credential list` must return zero passwords for every identity this spec creates.
2. **No public network paths by default.** Storage, ACR, Key Vault, and the workspace are created with `public_network_access_enabled = false`; access is via **private endpoints** and Private DNS zones. The online endpoint's public ingress is the single deliberate exception, and it is behind auth.
3. **Least privilege, scoped to the resource.** No `Owner`, no `Contributor` at subscription scope. Every role assignment names a specific scope and a specific built‑in role, justified in a comment.
4. **Everything encrypted, logged, and retained.** TLS 1.2 minimum, infrastructure encryption on storage, diagnostic settings on every resource → Log Analytics, 90‑day retention minimum.
5. **Immutable evidence.** `scores` container is append‑only and protected by an immutability policy; audit logs cannot be deleted by the runtime identity.
6. **Reproducible and attributable.** Every resource carries tags: `app=firmaware`, `env`, `owner`, `cost_center`, `data_classification`, `managed_by=terraform`. Deploy by **image digest**, never by tag.
7. **Fail closed.** Contract violations, gate failures, and policy denials stop the pipeline; nothing produces a success‑shaped default.

---

## 1. Deliverables

```
infra/azure/
  README.md                     decisions log + bootstrap + threat model summary
  bootstrap.sh                  idempotent: state RG/SA, OIDC app + federated creds
  backend.tf                    azurerm backend, blob state + lease locking
  versions.tf                   terraform >= 1.7, azurerm ~> 4.x pinned
  variables.tf  main.tf  outputs.tf
  modules/
    foundation/                 RG, Log Analytics, ACR, Key Vault, diagnostic settings
    network/                    VNet, subnets, NSGs, private endpoints, Private DNS
    storage/                    ADLS Gen2 account, containers, lifecycle, immutability
    identity/                   UAMIs, role assignments (least privilege, commented)
    workspace/                  Azure ML workspace, compute cluster, datastore
    endpoints/                  online endpoint + blue/green deployments, batch endpoint
    monitoring/                 alerts, action group, data collection, budget
  envs/dev.tfvars  envs/prod.tfvars
  policy/                       Azure Policy assignments (deny public blob, require tags)
azureml/
  environment.yaml              registered environment (references ACR image by digest)
  components/{validate,features,train,evaluate,register}.yaml
  pipeline-train.yaml           the training pipeline job
  endpoint-online.yaml          endpoint definition
  deployment-blue.yaml  deployment-green.yaml
  endpoint-batch.yaml  deployment-batch.yaml
  score.py                      online scoring entry script
deploy/azure/
  build.sh                      build + push to ACR, print digest
  deploy_endpoint.sh            create/update green at 0% traffic
  promote_traffic.sh            staged 10 → 50 → 100 with health checks between
  rollback_endpoint.sh          traffic flip to previous deployment
  rollback_model.sh             re-point batch deployment to a prior model version
  run_training.sh               submit the pipeline job, stream, return run id
  smoke_test.sh                 post-deploy verification (§6)
  security_check.sh             asserts the §0 non-negotiables against live state
.github/workflows/
  azure-ci.yaml                 PR: tests, lint, tf validate, tfsec/checkov, YAML validate
  azure-deploy-dev.yaml         merge to main
  azure-deploy-prod.yaml        tag v*, manual approval + digest match
```

---

## 2. Infrastructure (Terraform, `azurerm`)

### 2.1 Foundation
Resource group per env (`rg-firmaware-{env}`). **Log Analytics workspace** (90‑day retention) created first — every subsequent resource gets a `azurerm_monitor_diagnostic_setting` shipping logs and metrics to it, including the workspace, endpoints, storage, ACR, and Key Vault. **ACR** Premium (needed for private endpoints and immutable tags): `admin_enabled = false`, `anonymous_pull_enabled = false`, quarantine policy on, retention policy 30 days for untagged manifests. **Key Vault** in RBAC mode with `purge_protection_enabled = true` and soft delete 90 days.

### 2.2 Network
VNet with three subnets: `snet-compute` (Azure ML compute, service endpoints off, delegated as required), `snet-private-endpoints` (private endpoints), `snet-scoring` (managed endpoint outbound where applicable). NSGs deny inbound from Internet by default; only the documented Azure ML service tags are allowed. **Private endpoints for:** storage (`blob` and `dfs` sub‑resources), ACR, Key Vault, and the Azure ML workspace, each with its Private DNS zone linked to the VNet. Workspace uses **managed VNet isolation** with outbound rules enumerated — no blanket egress.

### 2.3 Storage
One ADLS Gen2 account per env, hierarchical namespace on, `min_tls_version = "TLS1_2"`, `allow_nested_items_to_be_public = false`, `shared_access_key_enabled = false` (**AAD only**), infrastructure encryption enabled, blob versioning on, soft delete 30 days for blobs and containers, change feed on for auditability.

| Container | Purpose | Controls |
|---|---|---|
| `data` | inputs | versioning; lifecycle → Cool after 90 d |
| `artifacts` | run outputs | versioning; legal‑hold capable |
| `scores` | append‑only predictions | **immutability policy** (time‑based, 7 years, unlocked in dev / locked in prod); runtime identity has write + read, **never delete** |
| `collected` | inference data collection | restricted read; drift jobs only |

### 2.4 Identity and RBAC
Three user‑assigned managed identities, each with a comment justifying every assignment:

| Identity | Role assignments (scoped) |
|---|---|
| `id-firmaware-{env}-workspace` | `Storage Blob Data Contributor` on `artifacts` + `collected`; `Storage Blob Data Reader` on `data`; `AcrPull` on ACR; `Key Vault Secrets User` on KV |
| `id-firmaware-{env}-endpoint` | `Storage Blob Data Reader` on `artifacts`; `Storage Blob Data Contributor` on `collected`; `AcrPull`; `AzureML Data Scientist` limited to the workspace |
| `id-firmaware-{env}-cicd` (federated to GitHub) | `AzureML Data Scientist` + `AcrPush` on ACR + `Storage Blob Data Contributor` on the tfstate container; **no** `Contributor`, **no** subscription‑scope roles |

Scores‑container assignment for the batch identity must be **`Storage Blob Data Contributor` minus delete** — implement with a custom role definition (`Storage Blob Data Appender`) granting `read`, `write`, `add`; explicitly **not** `delete`. A negative test proves deletion fails (§8).

### 2.5 Workspace and compute
Azure ML workspace: public access disabled, `high_business_impact = true` in prod (suppresses diagnostic capture of potentially sensitive data), customer‑managed key optional flag, linked to the Key Vault / storage / ACR / App Insights created above. Compute cluster: `min_nodes = 0`, `max_nodes = 4`, `idle_time_before_scale_down = 300s`, **no SSH**, system‑assigned identity off in favour of the UAMI, subnet‑attached.

### 2.6 Endpoints
Online endpoint with `auth_mode = "aad_token"` (key auth only if a consumer cannot do AAD, and then the key lives in Key Vault, never in the repo). Two deployments (`blue`, `green`) each pinned to an explicit model **version** and the ACR **digest**; instance type `Standard_DS3_v2`; `min_instances = 1` in prod, `0` in dev; `egress_public_network_access = "disabled"`. Data collection enabled → `collected` container. Batch endpoint with its own deployment, output to `scores`.

### 2.7 Policy (compliance as code)
Assign at the resource‑group scope: deny storage accounts with public blob access; deny public network access on PaaS; require the six mandatory tags; audit TLS < 1.2; deny creation of service‑principal keys where possible. Policy assignment IDs recorded in `infra/azure/README.md` with the compliance control each maps to (e.g. CIS Azure 3.x, ISO 27001 A.9/A.10).

---

## 3. Application changes (the only two permitted)

**3.1** `azureml/score.py` — new file, outside `src/firmaware/`:
```
init()  -> load the registered PyFunc once (model + preprocessor + decision logic)
run(payload) ->
    validate against schema.py in scoring mode
    422 with the named violation on contract failure   (never a default)
    return {risk_probability, risk_prediction, risk_band,
            unseen_categories, model_version, threshold}
```
`unseen_categories` must be populated, never silently empty — the out‑of‑corpus vendor case must be visible in the response body. Log the correlation id, **never the payload**, unless `high_business_impact` logging is explicitly enabled.

**3.2** `src/firmaware/io.py` — extend the existing URI shim to recognise `abfss://` alongside `gs://` and local paths, importing `azure-storage-file-datalake` **lazily**, exactly as `google-cloud-storage` is imported today. No other file in `src/firmaware/` changes; the acceptance tests from the pipeline spec must still pass unmodified.

---

## 4. Training pipeline (`azureml/pipeline-train.yaml`)

Five components sharing the registered environment: `validate → features → train → evaluate + gate → register`. Data input is a **versioned data asset**, not a path. The gate blocks `register` unless AUC ≥ baseline − 0.02, recall at the chosen threshold ≥ target, and zero contract warnings. Registration tags the model version with git SHA, image digest, data asset version, threshold, cost ratio, and every test metric. Submitted manually or by dispatched workflow — **never on merge**.

---

## 5. CI/CD

**`azure-ci.yaml` (every PR):** ruff → pytest (all pipeline‑spec acceptance tests) → `terraform fmt -check`, `validate`, `tflint` → **`checkov` and `tfsec` with failures blocking** → `az ml` YAML schema validation → container build (no push) → **Trivy image scan, blocking on HIGH/CRITICAL** → secret scan (`gitleaks`) on the diff.

**`azure-deploy-dev.yaml` (merge to main):** OIDC login → build + push, capture digest → `terraform apply -var-file=envs/dev.tfvars -var image_digest=…` (plan posted to the run) → deploy `green` at **0% traffic** → `smoke_test.sh dev` against the deployment‑specific route → `promote_traffic.sh` staged 10 → 50 → 100 with a health assertion between each step → `security_check.sh dev`. Any failure halts and leaves `blue` at 100%.

**`azure-deploy-prod.yaml` (tag `v*`):** identical, plus (a) GitHub **environment protection with required reviewers**, and (b) a hard check that the digest being promoted **is the digest currently serving in dev** — promote what was tested, never rebuild.

All workflows: `permissions: id-token: write, contents: read` and nothing more; pinned action SHAs, not floating tags.

---

## 6. `smoke_test.sh {env}` and `security_check.sh {env}`

**Smoke test:** POST the 5‑row fixture (including one out‑of‑corpus vendor row) to the green deployment's specific route; assert HTTP 200, five results, exactly one row with non‑empty `unseen_categories`, `model_version` equal to the registry version just deployed. Then POST a deliberately malformed record and assert **HTTP 422 naming the violation**. Print the model version and image digest so every deploy log states what is live.

**Security check (run in CI, fails the deploy):**
1. Zero service‑principal passwords on all identities.
2. Storage `shared_access_key_enabled == false`, `public_network_access == Disabled`, `min_tls_version == TLS1_2`.
3. ACR `admin_enabled == false`; latest image scan has no unresolved HIGH/CRITICAL.
4. No role assignment for FirmAware identities at subscription scope; none with `Owner`/`Contributor`.
5. Immutability policy present on `scores`; runtime identity delete on `scores` returns 403.
6. Diagnostic settings present on workspace, endpoint, storage, ACR, Key Vault.
7. All six mandatory tags present on every resource in the RG.

---

## 7. Rollback

| Class | Command | Target |
|---|---|---|
| Model (serving) | `rollback_endpoint.sh {env}` → traffic `blue=100 green=0` | **< 30 s** |
| Model (batch) | `rollback_model.sh {env} {version}` | < 5 min |
| Code / image | redeploy green pinned to the previous digest, shift traffic | < 10 min |
| Infra | `git revert` → CI re‑applies; state protected by blob versioning + lease lock | < 30 min |
| Data | restore prior blob version, re‑run `validate` | < 10 min |

Scores are **non‑rollbackable by design** — superseded, never deleted; the immutability policy enforces this at the platform level rather than by convention. Each class is rehearsed once in dev and the observed time recorded in `infra/azure/README.md`.

---

## 8. Acceptance criteria

1. `bootstrap.sh` + `terraform apply` from a clean subscription succeeds with no portal clicks; a second apply shows **zero diff**.
2. `security_check.sh` passes in both envs, and **fails** when any single control is deliberately broken (prove it for at least three controls).
3. A PR that breaks any pipeline‑spec acceptance test, or introduces a `HIGH` Trivy finding, or adds a secret, is blocked by CI.
4. Training pipeline fed a CSV with an out‑of‑vocabulary outcome **fails at `validate`**, registers nothing, and fires the alert.
5. Evaluate‑gate failure blocks registration; the registry gains no version.
6. Deploy to dev: green at 0% → smoke passes → traffic promotes to 100% → `blue` retained.
7. Tag `v0.1.0`: prod deploy waits for approval and rejects a digest that differs from dev's.
8. Malformed scoring request returns 422 naming the violation; out‑of‑corpus vendor row returns 200 **with** `unseen_categories` populated.
9. Runtime identity cannot delete from `scores` (403) and cannot read `data` with a shared key (keys disabled).
10. Rollback rehearsal: traffic flip completes in under 30 s and the endpoint provably serves the prior model version.
11. Every resource in the RG carries the six mandatory tags; Azure Policy compliance shows no violations.
12. Cost estimate recorded in `infra/azure/README.md`; dev with `min_instances = 0` stays under $40/month.

---

## 9. Notes for the implementing agent

- Prefer boring, explicit Terraform: no dynamic blocks where a static one reads clearly, one module per concern, outputs only what another module consumes.
- Every role assignment gets a one‑line comment naming *why that principal needs that role on that scope*. A reviewer must be able to audit RBAC from the code alone.
- `infra/azure/README.md` must contain: the decisions table (matching the style of the existing `infra/README.md`), a short threat model (what an attacker with a leaked GitHub token can and cannot reach), the compliance‑control mapping, the rollback rehearsal times, and the cost estimate.
- Do not add an MLflow server; the Azure ML workspace's native tracking is the system of record, and the local SQLite store remains a development convenience.
- RAG is out of scope. Do not create a corpus container, index job, or second endpoint.
