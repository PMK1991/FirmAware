# Spec — FirmAware on GCP: IaC, Deployment, CI/CD, Rollback

> Implementation specification for a coding agent. Companion to `FirmAware_ML_Pipeline_Spec.md` (v1.0) — that spec defines the application; this one defines how it ships. Self‑contained; where silent, choose the simplest option and record it in `infra/README.md`.
> Version 1.0 · 2026‑07‑22 · Scope: the **batch ML pipeline only** (validate / train / predict). No serving API, no Streamlit, no agents, no LLMs — FirmAware has none of these yet.

---

## 0. Plan (phases; each independently shippable)

| Phase | Deliverable | Done when |
|---|---|---|
| P1 | Containerize + run locally via Docker | `docker run … predict` reproduces local outputs bit‑for‑bit |
| P2 | Terraform baseline: project APIs, buckets, Artifact Registry, service accounts, IAM | `terraform apply` from clean state is idempotent; second apply = no changes |
| P3 | Cloud Run Jobs (`train`, `predict`, `validate`) + Cloud Scheduler for nightly predict | Manual `gcloud run jobs execute` succeeds end‑to‑end against GCS data |
| P4 | CI/CD: test → build → push → deploy (dev), tag‑gated promote (prod) | Merged PR auto‑deploys dev; tag `v*` deploys prod; both smoke‑tested |
| P5 | Rollback runbook rehearsed | Each rollback class executed once in dev and timed |

**Architecture (target):**

```
GitHub repo ── Actions (WIF, keyless) ──► Artifact Registry (image, by digest)
                                              │
   Cloud Scheduler ──► Cloud Run Job: predict ─┤
   (nightly, dev+prod)                         ├──► GCS buckets:
   Manual/CI trigger ─► Cloud Run Job: train ──┤      data/  artifacts/  scores/
                        Cloud Run Job: validate┘      (versioned, per env)
```

Design constraints carried from the pipeline spec: **no interactive input, no network calls from the app itself, deterministic runs, append‑only scores** — all of which is what makes it a clean batch workload. Note: this pipeline has **zero application secrets** (no DBs, no APIs); Secret Manager is provisioned empty for future use, and CI auth is keyless (WIF). No service‑account keys may exist anywhere.

---

## 1. Repository additions (create exactly this, on top of the existing tree)

```
firmaware/
  Dockerfile
  .dockerignore
  Makefile                      # build, test, run-local, deploy-dev shortcuts
  infra/
    README.md                   # decisions log + bootstrap instructions
    backend.tf                  # GCS state bucket (bootstrapped manually once)
    versions.tf                 # terraform >=1.7, google provider pinned
    variables.tf                # project_id, region, env, image_digest, alerts_email
    main.tf                     # module wiring
    modules/
      foundation/               # APIs, Artifact Registry, state outputs
      storage/                  # buckets + lifecycle + versioning
      iam/                      # service accounts + least-privilege bindings
      jobs/                     # 3 Cloud Run Jobs + Scheduler + alerting
    envs/
      dev.tfvars
      prod.tfvars
  deploy/
    build.sh                    # build + tag + push, prints digest
    run_job.sh                  # gcloud run jobs execute wrapper w/ --wait
    smoke_test.sh               # post-deploy check (see §5)
    rollback_model.sh           # repoint champion (see §6)
    rollback_image.sh           # repin job to previous digest (see §6)
  .github/workflows/
    ci.yaml                     # every PR: lint + unit + acceptance tests
    deploy-dev.yaml             # on merge to main
    deploy-prod.yaml            # on tag v*
```

## 2. Container (P1)

- Base `python:3.11-slim`; multi‑stage (builder installs deps, runtime copies venv). Non‑root user. `ENTRYPOINT ["python","-m","firmaware"]` — the job's `args` select `validate|train|predict`.
- Data paths become env‑configurable: `FIRMAWARE_DATA_URI`, `FIRMAWARE_ARTIFACTS_URI`, `FIRMAWARE_SCORES_URI` accepting `gs://` or local paths. Implement one small IO shim (`src/firmaware/io.py`) using `google-cloud-storage` **only when the URI is `gs://`** — local behavior unchanged, keeping pipeline‑spec acceptance tests green.
- `predict` append semantics on GCS: write each run as a new object `scores/scores_{scored_at}_{run_id}.csv` (object‑append via new objects, not rewrite) — analytics read the prefix. This *strengthens* the append‑only rule.
- Image tagged `{git_sha}` and `latest-dev`; **jobs always reference the immutable digest**, never a tag.

## 3. Terraform (P2) — resources and rules

**foundation:** enable `run, artifactregistry, cloudscheduler, storage, secretmanager, monitoring, logging` APIs; Artifact Registry (docker, regional, immutable tags on).

**storage:** per env (`{env}` prefix), three buckets, uniform access, **object versioning ON** for `artifacts` and `data`, lifecycle: noncurrent versions kept 90 days, scores kept forever:
- `firmaware-{env}-data` — input CSVs; humans/ingestion write, jobs read.
- `firmaware-{env}-artifacts` — layout below (§6 depends on it).
- `firmaware-{env}-scores` — jobs write, analysts read.

**iam:** three service accounts, least privilege, no basic roles:
- `sa-firmaware-jobs` — runtime for all three jobs: `objectViewer` on data, `objectAdmin` on artifacts + scores, `logWriter`, `metricWriter`. Nothing else.
- `sa-firmaware-deployer` — used by CI via **Workload Identity Federation** (repo‑scoped provider): `run.developer`, `artifactregistry.writer`, `iam.serviceAccountUser` on the jobs SA, storage admin on the tf‑state bucket.
- `sa-firmaware-scheduler` — `run.invoker` on the predict job only.

**jobs:** three Cloud Run Jobs (`firmaware-validate`, `firmaware-train`, `firmaware-predict`); train: 4 CPU / 8 GiB, timeout 30 min, `max_retries=0` (a failed training run must fail loudly, not retry into a different model); predict/validate: 1 CPU / 2 GiB, timeout 10 min, `max_retries=1`. Env vars per §2; image digest passed as `var.image_digest`. Cloud Scheduler: nightly predict per env (`0 2 * * *`, env TZ) with the scheduler SA. **Monitoring:** log‑based alert on any job execution failure and on the smoke test's contract‑violation exit code (1) → `alerts_email`.

**Rules:** remote state in a dedicated GCS bucket with versioning (bootstrapped once, documented); `terraform plan` must be clean on second apply; every resource carries `labels = {app="firmaware", env=var.env, managed_by="terraform"}`; no resource created outside Terraform except the state bucket and the WIF pool (bootstrap script `infra/bootstrap.sh`, idempotent, documented).

## 4. CI/CD (P4) — GitHub Actions

**`ci.yaml` (every PR + main):** ruff → pytest (all pipeline‑spec acceptance tests, incl. the contract‑rejection and Moxa OOD tests) → `docker build` (no push) → `terraform fmt -check` + `terraform validate` + `tflint`.

**`deploy-dev.yaml` (merge to main):**
1. Auth via WIF (no keys). 2. Build + push image; capture **digest**. 3. `terraform apply -var-file=envs/dev.tfvars -var image_digest=…` (plan output attached to the run). 4. Execute `firmaware-validate` job against dev data (`--wait`). 5. `smoke_test.sh dev`. Any step fails → workflow red, deploy halted; prior job revisions remain live (Cloud Run Jobs keep executing the last good digest until Terraform succeeds).

**`deploy-prod.yaml` (tag `v*`):** same steps against prod with two gates: a **required manual approval** (GitHub environment protection), and a hard check that the image digest being promoted **is the digest currently live in dev** (promote what was tested, never rebuild for prod).

**Model retraining is a deliberate act, not CI:** `train` runs via `deploy/run_job.sh train {env}` (or a manually‑dispatched workflow) — never on merge. Rationale: training changes model behavior without changing code; it gets its own audit trail (§6 layout) and its own approval in prod.

## 5. Smoke test (`deploy/smoke_test.sh {env}`)

1. Execute `firmaware-predict` with `--args --input gs://firmaware-{env}-data/smoke/upcoming_smoke.csv` (a 5‑row fixture incl. one Moxa row, committed to the repo and uploaded by Terraform).
2. Assert: exit 0; a new scores object appeared; it has 5 rows; exactly 1 row has non‑empty `unseen_categories`; `model_run` in every row equals the champion metadata timestamp.
3. Print the champion pointer (§6) and the image digest, so every deploy log states exactly *which model + which code* is live.

## 6. Rollback plan (P5) — four independent classes

**Artifact layout that makes model rollback trivial (required change to `train`):** every training run writes to `gs://…-artifacts/runs/{run_id}/` (model, preprocessor, feature_list, medians, metadata) and then atomically rewrites a single small pointer object `gs://…-artifacts/champion.json → {run_id, digest_of_metadata, promoted_at, promoted_by}`. `predict` reads the pointer, then loads that run's artifacts. Nothing is ever overwritten.

| Class | Trigger | Action | Command | Target time |
|---|---|---|---|---|
| **Model** | Bad scores, drifted threshold, wrong training data | Repoint champion to any previous `run_id`; next predict uses it | `deploy/rollback_model.sh {env} {run_id}` (lists runs w/ metrics if no id given) | < 2 min |
| **Code/image** | Bad deploy, runtime failure | Re‑apply Terraform with previous known‑good digest (each deploy logs its digest; keep last 10 in `infra/README.md` table appended by CI) | `deploy/rollback_image.sh {env} {digest}` → `terraform apply -var image_digest=…` | < 10 min |
| **Infrastructure** | Bad tf change | `git revert` the infra commit → CI re‑applies. State bucket versioning covers state corruption | git revert + merge | < 30 min |
| **Data** | Corrupt/wrong input CSV uploaded | GCS object versioning: restore prior generation of the object; re‑run validate to confirm contract passes | `gcloud storage restore` (documented per‑object in runbook) | < 10 min |

**Non‑rollbackable by design:** scores. They are append‑only history; a bad scoring run is *superseded*, never deleted — publish a corrected run and record the bad `run_id` in `infra/README.md`'s incident table. **Rehearsal requirement (P5):** each of the four classes executed once in dev, with the observed time recorded in the runbook.

## 7. Environments & config

Two envs, same project or split projects (agent's choice — record it): `dev` (nightly scheduler ON, relaxed alerting) and `prod` (scheduler ON, alerting to `alerts_email`, deletion protection on buckets, manual‑approval gate). `config.yaml` stays in the image; env‑specific values (URIs, schedule) come only from Terraform‑set env vars — one image, promoted by digest, is valid in both envs.

## 8. Acceptance criteria

1. Fresh project + `bootstrap.sh` + `terraform apply` → all green with no manual console clicks; second apply shows zero diff.
2. `ci.yaml` fails a PR that breaks any pipeline‑spec acceptance test (verify by intentionally breaking the label rule in a test branch).
3. Merge to main → dev deploy → smoke test passes; the workflow log contains image digest + champion run_id.
4. Tag `v0.1.0` → prod requires approval → deploys the *same digest* as dev (verify the gate rejects a mismatched digest).
5. Nightly scheduler executes predict in dev; scores prefix gains one object per day; no object is ever modified after creation (verify with bucket audit log).
6. All four rollback classes rehearsed in dev; model rollback completes in under 2 minutes and the next predict provably uses the restored `run_id`.
7. Training job with a CSV containing `ROLLBACK_REQUIRED` in outcomes **fails the job with exit 1** and fires the alert — the contract survives the trip to the cloud.
8. `gcloud iam service-accounts keys list` shows zero user‑managed keys on all three SAs.
9. Job runtime SA cannot delete from the scores bucket (verify with a negative test).
10. Total monthly cost at this scale (one nightly predict, occasional train) estimated in `infra/README.md` and under $15/env — this workload is tiny; if the estimate exceeds that, something is over‑provisioned.

## 9. Explicit non‑goals & carried notes

No serving API (add a Cloud Run *service* later if online scoring is needed); no MLflow server (local `mlruns/` stays a dev tool; the champion pointer + per‑run metadata folder is the system of record in GCP); no VPC‑SC/air‑gap in this phase — but note: if FirmAware later feeds the FirmGuard OT product, the compliance question (IEC 62443 / air‑gapped OT networks) applies to *that* integration, and this design deliberately keeps all state in three buckets + one pointer so a contained/VPC variant is a Terraform re‑target, not a redesign.
