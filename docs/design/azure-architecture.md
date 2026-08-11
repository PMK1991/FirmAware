# FirmAware on Azure — ML Architecture (Azure ML + Managed Online Endpoints)

> Design document only — **no implementation**. Azure counterpart to `FirmAware_GCP_Deployment_Spec.md`, covering the ML pipeline (`FirmAware_ML_Pipeline_Spec.md`).
> **Version 2.1 · 2026‑07‑22** — **scope narrowed to the ML layer only.** The RAG evidence layer is deliberately out of scope here; its design stands untouched in `FirmAware_RAG_HLD_LLD.md` and gets its own Azure design once ML is running. Diagram: `FirmAware_Azure_ML_Architecture.drawio`.

---

## 1. Scope and the two consequences of this decision

**In scope:** data contract, training, model registry, real‑time scoring, batch scoring, monitoring, CI/CD, rollback — everything needed to serve a risk probability.
**Out of scope (parked):** corpus storage, chunking/embedding/index jobs, explanation endpoint, Azure OpenAI. Nothing in the design below blocks adding them later; they attach as additional pipeline jobs and a separate endpoint.

Choosing Azure ML changes two things architecturally:

**1. FirmAware gains real‑time inference.** Every prior spec was batch‑only ("no serving API" was an explicit non‑goal). A Managed Online Endpoint means a caller can score a single upcoming deployment synchronously over HTTPS. The nightly bulk path does not disappear — it becomes a **Batch Endpoint** — so one registered model backs two scoring surfaces.

**2. The champion‑pointer pattern retires.** The blob `champion.json` indirection existed to make rollback a pointer rewrite. Azure ML provides that natively and better: **model versions** in the registry, and **blue/green deployments** behind one endpoint with a traffic percentage. Rollback becomes a traffic shift measured in seconds, with the previous deployment still warm. This is the one place where vendor coupling buys something real.

Preserved from the GCP design: deploy by immutable digest, append‑only scores, contract enforcement inside the application, training as a manual act, no user‑managed keys.

---

## 2. Service mapping (ML only)

| Concern | GCP (built) | Azure |
|---|---|---|
| Data lake | GCS buckets | **ADLS Gen2** — one storage account, containers `data` and `scores`; versioning + soft delete; WORM optional on `scores` |
| Data lineage | (none) | **AzureML Data assets** (versioned `uri_file` / `MLTable`) — every run records the exact data version it trained on |
| Training | Cloud Run Job `train` | **AzureML pipeline job** on a CPU **compute cluster** (min 0 / max 4, scale‑to‑zero) |
| Experiment tracking | local `mlruns/` | **native MLflow** in the workspace — same API, URI change only |
| Model store | `champion.json` pointer | **Model Registry** — versions + tags |
| Real‑time scoring | — (new) | **Managed Online Endpoint** with blue/green deployments and traffic split |
| Bulk scoring | Cloud Run Job `predict` (cron) | **Batch Endpoint**, scheduled → writes to `scores` |
| Container registry | Artifact Registry | **ACR** — immutable tags, referenced by digest |
| Identity | Service accounts | **User‑Assigned Managed Identity** — no key material by construction |
| Secrets | Secret Manager (empty) | **Key Vault**, workspace‑linked (still nothing to store: the ML pipeline has no application secrets) |
| CI/CD | GitHub Actions + WIF | **GitHub Actions + OIDC** → `az ml` CLI v2 with YAML specs in the repo |
| Observability | Cloud Logging/Monitoring | **Application Insights** + **Log Analytics** + Azure Monitor alerts → Action Group |
| Drift | (planned) | **Inference data collection** → **AzureML Model Monitoring** |

---

## 3. Training — AzureML pipeline job

Five components, one container image (the same image that runs locally, promoted by digest):

```
validate → features → train → evaluate + gate → register
```

- **validate** — the data contract: outcome vocabulary assertion, required columns, duplicate ids, parseable dates. Fails the pipeline with a named violation. This guarantee must survive every port.
- **features** — the four derivations, signed version jump.
- **train** — time‑based split, `Preprocessor` fitted on train only, both candidate models, cost‑ratio threshold sweep. Params/metrics/artifacts logged to MLflow automatically.
- **evaluate + gate** — metrics on the held‑out year; **registration is blocked** unless gates pass (AUC ≥ baseline − 0.02, recall at chosen threshold ≥ target, zero contract warnings). A failed gate ends the run without touching the registry.
- **register** — new **model version** bundling `model.joblib`, `preprocessor.joblib`, `feature_list.json`, `metadata.json`, tagged with git SHA, image digest, data asset version, threshold, cost ratio, and all test metrics. Those tags are what make a rollback decision informed rather than a guess.

Triggered manually or by a dispatched workflow — **never on merge**. Training changes model behavior without changing code, so it gets its own approval and audit trail.

---

## 4. Inference — Managed Online Endpoint

**Endpoint** `firmaware-score-{env}` — HTTPS, AAD or key auth, user‑assigned managed identity with `Storage Blob Data Reader` on artifacts.

**Blue/green deployments:** two named deployments behind one endpoint, each pinned to a **specific model version** and instance type (`Standard_DS3_v2`, autoscale 1–3; `min_instances=1` in prod to avoid cold start). New model → deploy as `green` at **0% traffic** → smoke test via the deployment‑specific route → shift **10% → 50% → 100%** → keep the old deployment at 0% for the rollback window, then delete.

**`score.py` contract:**
- `init()` — load model + preprocessor once from the registered model artifact.
- `run(payload)` — validate the record against `schema.py` in scoring mode, derive features, apply the **same `Preprocessor`** (one code path, as the ML spec requires), return `{risk_probability, risk_prediction, risk_band, unseen_categories, model_version, threshold}`.
- Contract violations return **HTTP 422 naming the violation** — the same failure the batch path raises, surfaced as a status code.
- `unseen_categories` is never silently empty: the Moxa case must be visible in the response body.

**Inference data collection** enabled on the deployment (inputs + outputs to blob). This is what finally makes the outcome loop possible — every served prediction is durably recorded with its model version, ready to be joined against real deployment outcomes when that recording exists.

**Batch Endpoint** `firmaware-batch-{env}` — same registered model, scheduled nightly, reads `data`, writes append‑only objects to `scores`. Bulk economics stay bulk; the online endpoint serves single contested deployments and any future UI.

---

## 5. CI/CD and rollback

**CI/CD** — GitHub Actions with OIDC (keyless). PR: lint + unit + acceptance tests + `az ml` YAML validation. Merge to main: build/push image → create green deployment at 0% → smoke test → shift traffic. Tag `v*`: prod, gated by environment approval **and** a digest‑match check (promote exactly what dev tested).

**Rollback — four classes:**

| Class | Mechanism | Target |
|---|---|---|
| **Model (serving)** | `az ml online-endpoint update --traffic "blue=100 green=0"` — previous deployment still warm | **< 30 s** |
| **Model (batch)** | Re‑point the batch deployment at the prior registered model version | < 5 min |
| **Code / image** | Deploy a new green pinned to the previous ACR digest, shift traffic | < 10 min |
| **Infra / data** | `git revert` → Terraform re‑apply (state in blob, lease locking); blob version restore + re‑run `validate` | < 30 min |

Scores remain **non‑rollbackable by design** — superseded, never deleted.

---

## 6. Trade‑offs accepted

- **Vendor coupling.** Registry + traffic split beats a hand‑rolled pointer, but it is Azure‑shaped. Portability is preserved where it's cheap: the container image, `schema.py`, `features.py`, `transform.py`, and the contract tests stay cloud‑agnostic; only orchestration YAML and promotion mechanics are Azure‑specific.
- **Cost floor rises.** Batch‑only on scale‑to‑zero was ~$15/env/month. A prod online endpoint with `min_instances=1` runs continuously — budget for it, or accept cold starts in dev with `min_instances=0`.
- **A serving surface is a new attack surface.** Auth, rate limiting, and payload validation now matter; private endpoints and the 422‑on‑contract‑violation behavior are the mitigations.

Open questions still owned by a stakeholder: the real FN:FP cost ratio that sets the serving threshold, single vs multi‑subscription isolation, and whether the OT‑contained (air‑gapped) variant is in scope for v1.

---

## 7. When RAG returns

Attaching the evidence layer later requires no rework of the above: add a `corpus` container, an index‑build pipeline job whose output registers as a **model asset** (inheriting the same versioning and gated promotion), and a **separate** online endpoint for explanation — separate because an LLM outage must never take down risk scoring. Design is already written in `FirmAware_RAG_HLD_LLD.md`.
