# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FirmAware is a firmware deployment-risk pipeline: it trains a cost-sensitive
GO/NO_GO classifier (logistic regression vs XGBoost, chronological tuning),
publishes it as a raw-input MLflow PyFunc, and batch/online scores upcoming
deployments. The same package and image run locally, on GCP (Cloud Run Jobs),
and on Azure ML (pipeline jobs, online + batch endpoints). `app.py` is a
read-only Streamlit page over already-written scores. `README.md` is the
authoritative long-form design doc.

## Commands

```powershell
python -m pip install -e ".[dev,app,train,rag]"   # whole repo; add ",azure" for abfss:// work
ruff check src tests                          # ruff is pinned exactly and has no config: defaults are the rules
python -m pytest -q                           # full suite (what CI runs)
python -m pytest tests/test_io.py -q          # one file
python -m pytest "tests/test_io.py::ClassName::test_name" -q   # one test (tests are unittest.TestCase classes)

python -m firmaware validate --input data\deployment_events.csv --mode training
python -m firmaware train --input data\deployment_events.csv --config config.yaml
python -m firmaware predict --input data\upcoming_deployments.csv
streamlit run app.py
mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000

python -m firmaware.rag lint                  # corpus vs data/corpus/manifest.json (+ id resolution if data/ has the CSV)
python -m firmaware.rag manifest --source "<where the docs came from>"   # re-derive after a reviewed corpus change
python -m firmaware.rag chunk --out outputs/chunks.jsonl   # per-type chunking; fails on any broken invariant
python -m firmaware.rag eval                  # routed retrieval + gate eval -> outputs/rag_eval.json; exit 1 on a failed gate
python -m firmaware.rag explain UPC_00045     # one deployment end to end; generates when .env configures a model
python -m firmaware.rag index                 # build a new Azure AI Search index, eval it, repoint the alias only if gates pass
python -m firmaware.rag eval-generation       # explain a fixed probe set with the real model; LLM-judge + deterministic gates
```

With `FIRMAWARE_SEARCH_ENDPOINT` (and the Foundry settings) in `.env`, `eval`,
`explain` and the page use Azure AI Search instead of the local BGE index; see
the `.env` comments. Azure calls are keyless via `az login` (Entra ID).

`app.py`'s Deployment Inspector gains a "Historical Evidence — RAG" section
only when `rag_available()`: `data/corpus/manifest.json` (or under
`FIRMAWARE_CORPUS_DIR`) exists, and either Azure AI Search is configured and
`azure.identity` is importable, or `sentence_transformers` and `pdfplumber` are
(local BGE). The Azure page image ships only the manifest, never the corpus
documents: chunk text and vectors come from the index, and Terraform sets the
Search/Foundry env vars. Pages without either backend render unchanged; a test
pins that. The evidence index loads on the first "Explain" click, never on page
load (about a minute and 1 GB for local BGE).

`eval` and `explain` need the ignored `data/*.csv` and the `notebooks` extra
(sentence-transformers/torch for BGE embeddings, `anthropic` for generation);
without them they exit 2 ("not run"), which is neither a pass nor a failure.

Docker (see `Makefile` for `build`, `run-local`, `validate-local`, `train-local`):
the `Dockerfile` has stages `builder` → `runtime` → `azureml` and `app`.
Always pass `--target` explicitly — a bare `docker build` builds the last stage.
`azureml` needs `--build-arg PIP_EXTRAS=train,azure`; `app` needs the `app` and
`azure` extras. Each stage has an import guard that fails the build otherwise.

Infra checks CI runs (in `infra/gcp` or `infra/azure`):
`terraform fmt -check -recursive`, `terraform init -backend=false && terraform validate`,
`tflint --init && tflint --recursive`. Azure ML YAML specs:
`python .github/scripts/check_azureml_specs.py`.

## Architecture

- **`src/firmaware/` is cloud-agnostic and must stay that way.** No module
  imports a cloud SDK at top level; `io.py` lazily imports
  `google-cloud-storage` for `gs://` and the Azure datalake client for
  `abfss://`, otherwise uses local paths. All cloud-specific code lives in
  `deploy/<cloud>/` and `infra/<cloud>/` (Terraform modules per concern).
- **One preprocessing path.** Training, local `predict`, the MLflow PyFunc
  (`model.py`), the Azure `score.py`, and the Streamlit page all go through
  `schema.validate` → `features` derivation → `transform.Preprocessor.apply`.
  Transform order is fixed: impute → encode → align → scale (align before scale
  so missing features are filled in raw space). Don't add a second copy of any
  of this logic; `app.py` imports schema/features/io rather than restating them.
- **Data contract** (`schema.py`): training = 30 columns, scoring = 27 (drops
  `deployment_outcome`, `time_to_failure_hours`, `rollback_required`, which are
  post-deployment leakage). Identifiers and `deployment_date` never enter the
  feature matrix. Unseen categories are scored as all-zero one-hot and reported
  as OOD, never silently aliased. Violations raise `ContractViolation` — the
  codebase fails closed rather than returning success-shaped defaults.
- **Decision line**: one threshold chosen by minimizing
  `FN * cost_ratio_fn_fp + FP` on pooled out-of-time CV predictions, persisted
  in `metadata.json`. GO/NO_GO and LOW/MEDIUM/HIGH bands all derive from it.
- **Training** (`train.py`): outer chronological split → expanding time-series
  folds → seeded randomized search (config in `config.yaml`) → best per family
  → holdout comparison → champion. Nested MLflow runs per candidate; local
  artifacts are staged per run and promoted atomically only after MLflow
  logging/registration succeeds.
- **Environment contract**: `FIRMAWARE_DATA_URI`, `FIRMAWARE_ARTIFACTS_URI`,
  `FIRMAWARE_SCORES_URI` (plus `FIRMAWARE_UPCOMING_URI` for the page) switch
  between local/GCS/ADLS; standard `MLFLOW_*` vars redirect tracking.
- **Scores are append-only.** Remote writers create a new object per run and
  never overwrite; local `predict` appends and refuses a mismatched header.
- **Model promotion differs by cloud**: GCP uses immutable run prefixes plus a
  `champion.json` pointer (digest-verified on load); Azure uses Model Registry
  versions and blue/green traffic on a Managed Online Endpoint, so there is no
  pointer on Azure.
- **Streamlit page** falls back to the committed fixtures in `demo/` when no
  scores are found — including when an ADLS prefix matches nothing — so an
  empty source renders sample data rather than failing.

- **GenAI / RAG evidence layer** (in progress, Azure-only; plan in
  `docs/design/rag-plan.md`, corpus design in `docs/design/rag-hld-lld.md`).
  `src/firmaware/rag/` follows `io.py`'s rule: a cloud SDK only inside the
  function that calls the cloud, never at module level. `azure-identity` is
  imported only inside functions of `generate.py` (Foundry) and
  `azure_search.py` (Search, via REST), so chunking and eval still run with no
  credentials (a test pins both). Search readers query an alias; `rag index`
  builds a timestamped index, evals it and repoints the alias only if gates
  pass. The Search service, Foundry account and model deployments are
  Terraform (`infra/azure/modules/evidence/`), keyless, and the page identity
  holds only Search Index Data Reader and Cognitive Services User. Flow: `flags` (deployment flags in the corpus's own
  definitions, which deliberately differ from `app.py` on three flags) →
  `retrieval` (six routes, one per question a document type answers, behind a
  filtered-search `SearchBackend`; `LocalIndex` is the numpy backend) → `gate`
  (deterministic verdict) → `generate` (pluggable `Generator`: Foundry-hosted Llama-3.3-70B via Entra ID first, Claude second, picked by `generator_from_env`,
  plus grounding checks) → `explain`. `explain` never calls the generator for
  `insufficient_evidence`/`low_confidence`. `sentence_transformers`,
  `anthropic` and `dotenv` are imported only inside functions (a test enforces
  it). The 69-doc corpus lives under `data/corpus/` (excepted from the `data/`
  ignore rule) with a sidecar `manifest.json` as its metadata contract. Whether
  it may be published is undecided, so tests that need it skip when it is
  absent, and the lint tests use the synthetic `tests/fixtures/rag_corpus/`.

## Things that bite

- **Tests assert on infra and deploy files.** `tests/test_azure_deployment.py`
  and `tests/test_deployment.py` read the Dockerfile, Terraform, workflow YAML,
  shell scripts, smoke fixtures, `.trivyignore`, etc. and enforce security posture (RBAC scopes, no scores
  delete permission, non-root container, checkov skips must be inline `CKV_ID:reason`, trivy
  suppressions only in the ignore file). Editing `infra/`, `deploy/`, or
  `.github/` can break `pytest`.
- `tests/test_notebooks.py` requires every notebook in `notebooks/` to be saved
  **executed** with no error outputs. Clearing outputs fails the suite.
- Dependency pins in `pyproject.toml` are deliberate and commented: MLflow must
  stay on 2.x (Azure ML's tracking server lacks the MLflow 3 logged-models
  API); numpy is exact in `train` but a range in the base for the Streamlit
  Community Cloud deploy (Python 3.13). Read the comment before bumping.
- Send numeric columns as `float64` to the served PyFunc; its signature types
  nullable numerics as `double` and MLflow won't cast `int64`.
- XGBoost is run with one worker and a fixed seed for determinism; keep it that way.

## Conventions

- Code comments and commit messages explain *why* (the constraint or failure
  that forced a choice), not what. Match that density when editing.
- Design specs live in `docs/design/`; the `FirmAware_*.md` files at the repo
  root are older/working copies of the same specs.
