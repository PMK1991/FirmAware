# FirmAware RAG — Plan (Azure)

> Phased plan for the evidence layer. Target: **Azure only**. Supersedes the index and retrieval sections of `docs/design/rag-hld-lld.md`; the corpus taxonomy, chunking strategies, and eval design in that document stand unchanged.
> Version 1.0 · 2026‑08‑01 · Prerequisite: the ML layer is deployed and its endpoint is serving.

---

## 1. Decisions locked

| Decision | Choice | Consequence |
|---|---|---|
| Cloud | Azure only | No GCP RAG path. `infra/gcp` and `deploy/gcp` are untouched by this work. |
| Corpus | Reuse FirmGuard's 69 documents | Comes with `doc_id`, `vendor`, `document_type`, and `source_deployment_ids` — the last one yields a gold-standard retrieval eval set with zero hand labelling (§4.3). Synthetic, but numerically grounded in the same 20k records the model trains on. |
| Vector store | **Azure AI Search** | Replaces FAISS. Changes promotion, rollback, cost, and — usefully — makes hybrid retrieval native (§2). |
| Generation | **Azure OpenAI over a private endpoint** | Model call stays inside the tenant boundary; managed identity auth, no keys. This is the strongest available answer to the OT/air‑gap question open since the FirmGuard analysis. |
| Serving | Separate Managed Online Endpoint (`firmaware-explain-{env}`) | An LLM or index outage must never take down risk scoring. |

---

## 2. What Azure AI Search changes versus the FAISS design

Four consequences, two of them improvements and two of them costs. Stating them now so nobody rediscovers them in P2.

**Gained: hybrid retrieval stops being aspirational.** The lectures computed a `retrieval_mode` per chunk (vector / hybrid / vectorless) and never wired a router — every prior FirmGuard revision lists it as planned. AI Search does BM25, vector, and semantic ranking natively, so `retrieval_mode` becomes a query‑shape decision rather than an unbuilt component: policy and matrix lookups run as filtered keyword queries with **no embedding call at all**, narrative questions run vector, advisories run hybrid.

**Gained: metadata filtering moves server‑side.** The FirmGuard code over‑fetched ×4 and filtered chunks in Python because `retriever.py` was deliberately filter‑free. With filterable fields (`vendor`, `document_type`, `evidence_strength`, `provenance`) the filter is part of the query, which removes a class of "filtered away everything, returned too few" bugs.

**Lost: the index is no longer a model asset.** FAISS-as-a-model-asset gave versioning, tagged promotion, and sub‑30‑second rollback for free, mirroring the risk model. The equivalent on AI Search is **index aliases**: build `firmaware-corpus-{yyyymmdd-hhmm}`, run eval against it, then repoint the alias. Rollback is an alias repoint — same shape, same speed, different mechanism. This must be built deliberately; it is not the default flow, and "reindex in place" is the trap to avoid.

**Lost: the sub‑$40 dev cost floor.** Basic tier is roughly $75/month per environment, running whether or not anything queries it. The Azure implementation spec's cost acceptance criterion needs restating for RAG environments. Consider a single shared Search service across dev and prod with separate indexes if that number matters more than environment isolation — and record the trade‑off if so.

**Still open (decide in P2):** embeddings via AI Search *integrated vectorization* (Azure OpenAI `text-embedding-3-small`, one less moving part, couples chunking to the indexer) versus computing BGE‑small in the pipeline and pushing vectors (keeps embedding portable and reproducible, matches the lectures). Recommendation is integrated vectorization since the design is already Azure‑committed — but the eval in P3 is what should settle it, by running both.

---

## 3. Repository placement

Consistent with the split just built: portable logic shared, cloud specifics under `deploy/azure/`.

```
src/firmaware/rag/          parse, chunk, chunk-invariant tests, eval harness
                            pure Python, no cloud SDK, unit-testable offline
deploy/azure/rag/           AI Search index schema + alias management,
                            indexer/skillset definitions, Azure OpenAI client,
                            explain endpoint scoring script
infra/azure/modules/search/ Search service, private endpoint, RBAC, diagnostics
infra/azure/modules/openai/ Azure OpenAI account, deployment, private endpoint
.github/workflows/azure-rag-*.yaml
```

Rule carried from the ML work: nothing in `src/firmaware/` imports an Azure SDK. Chunking and eval must run on a laptop with no cloud credentials, because that is what makes the chunk invariants cheap to test.

---

## 4. Phases

Each phase is independently shippable and independently useful.

### P0 — Corpus intake and contract *(smallest, do first)*
Copy the 69 documents into `data/corpus/`. Write the **document metadata contract**: `doc_id`, `document_type`, `vendor`, `evidence_strength`, `source_deployment_ids`, `provenance` (`internal_derived` | `external_sourced`). Add a lint that fails on a missing field, an unknown `document_type`, or any reference to an upcoming `UPC_` id — the corpus is historical evidence only.
**Done when:** all 69 documents pass the lint, and the `source_deployment_ids` across the corpus resolve against `deployment_events.csv`.

**Status (2026‑09‑27): done**, with the contract reshaped by what the corpus actually is. `python -m firmaware.rag lint` passes and every well‑formed `DEP_*` id resolves. See §4.1 for what that took; three findings change P3 and P4.

### 4.1 P0 findings — the corpus against the plan's assumptions

**The contract is a sidecar, not front‑matter.** 14 of the 69 documents are PDFs, which cannot carry front‑matter, and the markdown ones spread their metadata over three inline layouts. The contract is `data/corpus/manifest.json`: derived once by `python -m firmaware.rag manifest`, reviewed, committed. The lint re‑derives each record from the document's filename and body and fails on any disagreement, including a content hash, so an edited document fails until someone reviews the change. The PDFs stay PDFs: P1 has to reproduce their structural loss, so converting them would hide the case it tests for.

**Decisions the contract forced:**

| Question | Decision |
|---|---|
| Where the corpus lives | `data/corpus/`, excepted from the `/data/*` ignore rule so CI can assert chunk invariants over it. The deployment CSVs stay ignored, so id resolution runs only where they exist (test skips with a reason). |
| META has no id | `FG-DOC-000`, the slot the numbering leaves free, which keeps the `FG-DOC-\d+` citation contract total. |
| `document_type` vocabulary | `corpus_index.csv`'s names: `Postmortem`, `Advisory`, `RiskNote`, `Runbook`, `Policy`, `Matrix`, `Transition`, `CrossVendor`, `META`. |
| Vendor scope | `vendor` is a vendor name, `cross-vendor`, or `vendor-neutral`; `vendors` is the list (one, the pair, or empty). **P3's vendor‑match gate must count only non‑empty `vendors`**: a policy or matrix is not evidence that a vendor is covered. |
| Malformed ids | Only `DEP_\d{6}` is valid. Six short refs (`DEP_00003`, `DEP_00004`, `DEP_00118`, `DEP_0143`, `DEP_05190`, and `DEP_00`, where FG‑DOC‑010's PDF text stops mid‑sentence at the end of its last page, confirmed by two parsers) cannot be padded safely. They are recorded as `malformed_refs`, kept out of the gold set, and any new one fails the lint. |
| Upcoming‑id lint | `\bUPC_\d+`, over extracted PDF text too. META states the rule "(UPC_ prefix)" in prose, which is not a breach. |

**Finding 1: there is no independent slice, so P4's provenance weighting does nothing.** Every document declares itself derived from deployment data ("not an official vendor publication", advisories included), and META records that the corpus was generated by Gemma over the training records. All 69 are `internal_derived`. ML↔RAG agreement is therefore one signal read twice, across the whole corpus rather than only the postmortems. Weighting stays in the design but has nothing to act on until genuinely external documents (real vendor PSIRT advisories, CVE records) are added. Until then, the explanation must not present corpus agreement as corroboration of the score.

**Finding 2: the free gold set is thinner than assumed, and partly wrong.** META says every document carries a list of source deployment ids. In practice there is no such field: ids appear in body prose, about 3 per document (191 references, 120 unique), and none at all in the 4 matrices, 5 policies and META. Measured against the historical rows they cite:

| Type | Docs | Ids per doc | Vendor match | Device‑family match |
|---|---|---|---|---|
| Advisory, Runbook, Transition, CrossVendor | 34 | 3.0–3.8 | 1.00 | 1.00 where named |
| Postmortem | 13 | 2.8 | 0.95 | 0.92 |
| RiskNote | 12 | 4.0 | 0.75 | 0.58 |
| Policy, Matrix, META | 10 | 0 | n/a | n/a |

The misses are concentrated: FG‑DOC‑036 (a Siemens PLC note) cites only Schneider PLC rows; FG‑DOC‑037 cites mostly Cisco and Schneider; FG‑DOC‑013, 030 and 031 each cite Cisco rows. "Relevant by construction" holds for most types and fails for RiskNotes. Consequences for P3:
- Build the gold set only from cited ids whose row matches the document's vendor (and device family where it names one). `python -m firmaware.rag lint` prints this table, so the gold set's quality stays visible as the corpus changes.
- ~3 queries per document is a small sample: a per‑type recall@5 over 12 docs moves in steps of about 3%.
- "≥ 0.6 on every type slice" is undefined for Policy, Matrix and META, which have no gold queries. Those types need their own check: the vectorless route's lookup tests (HLD acceptance criteria 5–6), not recall.

**Finding 3: the evidence‑strength totals check out.** META claims 58 HIGH, 9 MEDIUM and 1 LOW, and the documents carry exactly that once PDF text is included. The lint asserts it, which is also how it notices a PDF extracting to nothing.

### P1 — Parse and chunk, with invariants as tests
Nine typed strategies exactly as specified in the HLD/LLD: postmortems by `##`, advisory tables atomic, policies one rule per chunk *with its exception clause*, runbook steps in groups of four, matrices and META whole. Markdown canonical; PDFs pass through with the known structure‑loss caveat and a warning when a document's chunk count deviates more than 50% from its type's median.
**Done when:** ~409 chunks produced, every §3 invariant asserted as a test, and the PDF‑collapse case is reproduced and flagged rather than silently accepted.

**Status (2026‑09‑27): done.** `python -m firmaware.rag chunk` gives **377 chunks** with zero invariant violations. Every invariant is asserted over the whole corpus, and each one also has a test showing it fires on the defect it names. Per‑type counts are pinned to a ±10% baseline in `tests/test_rag_chunker.py`.

Why the total is 377, not ~409. The target moved deliberately: the lecture chunker's 409 includes defects that are now fixed. Each row below comes from a per‑document diff against the lecture's `faiss_metadata.json`, not from reasoning about the strategies:

| Type | Lecture | Now | Per document | Cause |
|---|---|---|---|---|
| Postmortem, Transition, CrossVendor | 220 | 195 | every md doc −1 | A title line with nothing under it is folded into the metadata that follows, not emitted as a title‑only chunk. |
| Advisory | 66 | 54 | md 10 → 8; PDF 1 → 1 | The same fold, plus the separate title and footer paragraphs. |
| Policy | 31 | 27 | md 14 → 12; PDF 1 → 1 | One title‑only chunk folded. The standalone Exception Process chunk is dropped because every rule chunk now carries it; the lecture's rule chunks never contained the exception at all. |
| Runbook | 63 | 72 | md 9 → 9; PDF 6 → 9 | Entirely the three PDF runbooks, which now keep their section structure (below). Balancing (5 → 3+2, never 4+1) changes chunk *contents*, not the count. |
| RiskNote, Matrix, META | 29 | 29 | unchanged | |

Decisions:
- **Exceptions are policy‑wide, not per‑rule.** These policies state their exceptions once, in a section governing every rule. So "exception clause stays with its rule" means each rule chunk carries the whole Exception Process. A policy with rules but no such section is not split at all.
- **Runbooks split on numbering, not on `##`.** Step numbering survives PDF extraction and markdown headings do not. Splitting where the numbering restarts at 1 keeps FG‑DOC‑038/042/044 structured. Wrapped PDF lines are rejoined only while the step is mid‑sentence; otherwise the Quick Reference table after the last step gets read as part of that step.
- **Collapse is labelled, not guessed at.** A strategy that finds none of its structure returns the document as one `whole` chunk. The per‑type rules exempt `whole` chunks, and `structure_warnings` reports each one by name. The 11 flagged documents are exactly the PDF postmortems (2), advisories (6) and policies (3); no markdown document is flagged. The deviation median is taken over markdown documents only: with half the advisories collapsing to 1, a median over both formats sits between the two groups and flags both.
- **The chunk record adds `vendors`** to the HLD's shape, so the vendor‑match gate can tell a cross‑vendor pair from a vendor‑neutral policy (§4.1).

Carried into P3: the 11 collapsed documents are retrievable only as whole‑document blobs, including every policy rule behind the three PDF policies. That affects the vectorless route's lookup tests. Re‑authoring those PDFs as markdown (the HLD's mitigation) is the fix, and it should be measured in P3 before anyone does it. Some documents' generated text is also truncated mid‑sentence (FG‑DOC‑005's Lessons Learned, FG‑DOC‑010's final paragraph). Chunking preserves this; nothing downstream can repair it.

### P2 — Index and alias‑based promotion
Terraform the Search service (private endpoint, RBAC, diagnostics to Log Analytics) and the Azure OpenAI account. Define the index schema: searchable text, filterable metadata, vector field. Build to a timestamped index name; promote by alias.
**Done when:** a full rebuild produces a new index, the alias still points at the old one until promotion, and an alias repoint is demonstrated and timed.

**Status (2026‑10‑21): built, with three departures from the text above.**
- **Free tier in dev.** `srch-firmaware-dev-fa7k` runs Search Free: $0, one per subscription, 50 MB, no SLA, no private endpoint. 377 chunks with 1536‑dimension vectors take about 3 MB. prod keeps `basic`.
- **Embeddings from Foundry, pushed by the build.** `text-embedding-3-small` on the Foundry resource embeds chunks once at build time and queries at run time (`foundry_embedder`). Search holds the vectors but does no vectorization itself, so it needs no identity of its own and no skillset. The page no longer loads BGE or torch.
- **Aliases are a preview API.** `/aliases` exists only in `2025-05-01-preview`; GA `2024-07-01` rejects it. `azure_search.py` pins the preview version for all calls.

`python -m firmaware.rag index` builds `firmaware-corpus-<UTC stamp>`, runs the P3 eval against the *new* index, repoints the `firmaware-corpus` alias only if every gate passes, and keeps the newest two indexes: the live one and its rollback. A failed build is left unpromoted for inspection. First build: recall@5 0.893 on the evidence route (Advisory 1.00, Postmortem 0.79, RiskNote 0.88; BGE was 0.957), runbook and transition 100% when they apply, cross‑vendor 63%, identical verdicts, negative controls 3/3, so it was promoted. Each Search request is slower than the local index; the 164‑query eval took 18 minutes.

Everything is keyless: Search runs RBAC only (local authentication disabled in Terraform), and the page holds Search Index Data Reader on that one service. Repointing the alias is not yet timed; that belongs to the P5 rollback rehearsal.

### P3 — Retrieval, confidence gate, and eval *(the phase that determines whether any of this works)*
Rich query construction from the scored deployment row — vendor, device, firmware from→to, active flags, CVSS, criticality, deployment type — never a bare topic string. Mode routing per §2. Then the **deterministic confidence gate**: ≥3 vendor‑matched chunks, ≥2 distinct documents, ≥⅓ of active flags grounded in the retrieved text, and a score threshold calibrated per backend (the original 0.45 cosine threshold never fires; see below). Four verdicts: `insufficient_evidence` | `low_confidence` | `concern_found` | `no_flag_evidence` (renamed from `no_concern`; see P4).

Eval, all automated, gating alias promotion:
- **Gold set for free:** for each document, build rich queries from its `source_deployment_ids` rows; that document is relevant by construction. Report **recall@5 and MRR sliced per document type and per vendor** — the four writing styles fail differently, and the aggregate hides it.
- **Negative controls:** an out‑of‑corpus vendor probe must return zero vendor matches and `insufficient_evidence`; scoring any confident answer is a test failure. A shuffled‑flag probe must land `low_confidence`, not `concern_found`.
- **Promotion gates:** recall@5 ≥ 0.8 overall, ≥ 0.6 on every type slice, negative controls 100%.
**Done when:** the gates run in CI, block promotion on failure, and a baseline report is committed.

**Local rehearsal (2026‑09‑27), `notebooks/06_rag_local.ipynb`.** The lecture stack (BGE‑small, exact cosine, the S17 gate) over the P1 chunks, with 164 gold queries across 55 documents and FirmGuard's nine flag definitions (the ones the corpus was written from). Five results change this phase's design before any Azure spend:

- **A single query cannot reach every document type.** With the vendor filter, recall@5 is 0.97 for Advisory, 1.00 for RiskNote and 0.91 for Postmortem, but 0.00 for Runbook, Transition and CrossVendor. Their gold documents sit at median rank 26.5, 25.5 and 52, behind "why" documents that fill the top five. Mode routing is therefore required, not an optimisation: runbooks by vendor and device, transitions by vendor and jump class, cross‑vendor by dependency. The `recall@5 ≥ 0.8 overall` gate (0.54 locally) has to become a gate per route.
- **Similarity cannot tell this deployment's evidence from another's.** Among the top 20 hits, gold chunks score a median 0.859 and other chunks 0.845; none falls below 0.45. A Moxa query, with no Moxa document in the corpus, still scores 0.82–0.83. Only vendor metadata separates them. Before recalibrating, try BGE's query‑instruction prefix (never used in the lectures) and a margin over the corpus baseline. On AI Search the check must be redesigned anyway, because hybrid returns fused rank scores, not cosines.
- **The gate, as specified, reduces to vendor presence.** Across the 55 upcoming deployments, the count, similarity and distinct‑document checks never fired, and flag grounding fired twice. Its checks need real discriminating power, not just a place in the pipeline.
- **Hybrid alone does not fix recall.** A hand‑rolled BM25 + vector reciprocal‑rank fusion scored 0.50 against 0.54 overall: better on postmortems, worse on advisories, and no help on the zero‑recall types. AI Search's hybrid still gets its own P3 measurement; the local evidence points to routing.
- **Collapsed PDFs score higher on document recall (0.96 against 0.47)** because whole‑document chunks carry the metadata header the query matches. Add chunk‑level precision to the eval, so the collapse cannot pass as a win.

The negative controls pass. The real Moxa deployment lands `insufficient_evidence` with zero generation calls, and a flag the corpus never discusses lands `low_confidence`.

**Status (2026‑10‑14): the offline half is built** in `src/firmaware/rag/` (`flags`, `retrieval`, `gate`, `generate`, `explain`, `evaluate`), cloud‑free and tested without torch, network or corpus. `python -m firmaware.rag eval` grades it; `python -m firmaware.rag explain UPC_…` runs one deployment end to end. Retrieval sits behind a filtered‑search interface (query, document filter, top‑k), the shape AI Search exposes, so the Azure backend replaces `LocalIndex` without the routes changing.

Routing, as built, with its measured result (BGE‑small, 164 gold queries, 55 upcoming deployments):

| Route | Documents | How | Gold result | Upcoming coverage |
|---|---|---|---|---|
| evidence | Postmortem, Advisory, RiskNote | vector, vendor filter | **recall@5 0.957**, MRR 0.649 (Advisory 0.97, Postmortem 0.91, RiskNote 1.00) | 98% |
| transition | Transition | vendor + jump band, then vector | cited doc returned 100% | 13% |
| runbook | Runbook | vendor + device family, vectorless | cited doc returned 100% | 75% |
| cross_vendor | CrossVendor | primary vendor, then vector | cited doc returned 58% when applied | 67% |
| policy | Policy | active‑flag keywords, vectorless, rule chunks before collapsed PDFs | — | 51% |
| matrix | Matrix | vendor named in it, vectorless | — | 98% |

- **Routing does not raise evidence recall; it makes the other types reachable.** The notebook's single filtered query scored the same 0.957 on these 93 evidence queries. Its overall 0.54 was lower only because it counted runbook, transition and cross‑vendor queries that one query cannot reach. What routing adds is those documents: runbooks and transitions whenever they apply, cross‑vendor reports 58% of the time.
- **Grounding on evidence‑only text changed no verdict on this batch.** The upcoming verdicts are the same as the notebook's: 27 `no_flag_evidence`, 25 `concern_found`, 2 `low_confidence` and 1 `insufficient_evidence`. In practice the gate is still vendor presence plus two grounding misses.
- **Only the evidence route is gated.** The runbook and transition routes select by metadata, so scoring them on their own citations would pass by construction. They report coverage and "returned the cited document when it applied" instead. All gates pass: recall@5 0.957 overall and ≥ 0.91 on every type, negative controls 3/3 with zero generator calls.
- **The jump bands are confirmed by the corpus, not fitted to it.** Major is [5, 8) and extreme is [8, ∞) on `version_jump_magnitude`. Counting failed historical rows in those bands reproduces each transition summary's stated record count: 470/470, 234/234, 58/58, 71/71 and 96/96 exactly, the other three within one. The manifest now carries `transition_class`, checked by the lint against each document's "Transition Category".
- **The cross‑vendor route cannot pick the pair.** The scoring contract records how many cross‑vendor dependencies a device has, never with which vendor, and 89% of historical deployments have one. The route therefore returns the vendor's own cascade reports and is never gated. FirmGuard's `deployment_dependencies.csv` does map devices to dependent vendors, but it lies outside FirmAware's 27‑column contract. Using it is an open decision, not wired in.
- **The gate no longer counts policy text toward flag grounding.** Grounding reads only the vendor‑matched evidence route, because every policy chunk names most flags. The score threshold is off by default (`GateConfig.min_mean_score`), to be calibrated per backend.
- **Not yet built:** chunk‑level precision in the eval, the LLM‑judge faithfulness rubric, the BGE query‑prefix experiment, and the AI Search backend (P2).
- **P3's done‑when is not met.** The gates run locally, not in CI. CI cannot run them whatever is decided about the corpus: the deployment CSVs are ignored, BGE needs torch, and the baseline report lands in the ignored `outputs/`. They become CI gates when they run against the AI Search index in the P2/P5 workflows, with the report committed or published as an artifact.

### P4 — Generation

**From the local rehearsal, and how the offline build settled each point:**
- 18 of 46 HIGH‑band deployments raise none of the nine flags, so the evidence cannot account for their score. **Done:** the verdict is now `no_flag_evidence`, not `no_concern`, and for that verdict `explain()` adds an instruction telling the generator that the evidence does not explain the score and must not be credited with it. A test asserts the instruction is sent.
- The page and the corpus define three flags differently. `app.py` counts a repeat‑failure device from 1 past failure (FirmGuard: 3), network stress from 0.70 (0.65), and any major‑version change (upgrades only). **Decided:** the RAG layer uses the corpus's definitions (`firmaware.rag.flags`, documented there) and `app.py` is unchanged. An explanation and the page's flag panel can therefore disagree on those three flags until one side changes.
- **Done:** generation sits behind a two‑method `Generator` interface, with a Claude Haiku implementation (`claude-haiku-4-5`, the lecture precedent; reads `ANTHROPIC_API_KEY` from the gitignored `.env`). The deterministic grounding checks (citations, exact‑token numbers, borrowed flags) are module code with tests proving each one fires. `explain()` never calls the generator for `insufficient_evidence` or `low_confidence`; the tests assert it with a counter. Generation itself has not yet run against the real corpus: no key is configured.
- **Decided (2026‑10‑21): an open model on Azure AI Foundry, not Azure OpenAI.** Llama‑3.3‑70B‑Instruct (version 9) runs as a Global Standard, pay‑per‑token deployment on `ais-firmaware-dev-fa7k` in `firmAware` (East US). It is called through the OpenAI‑compatible `/openai/v1/chat/completions` route with an Entra ID token (`FoundryGenerator`), so no key exists: `az login` locally, a managed identity in Azure. Access is the Cognitive Services User role, scoped to that one resource. The resource and deployments were created with the Azure CLI, then codified in `infra/azure/modules/evidence`. `evidence_imports.tf` adopts the hand‑made dev resources rather than recreating them. A read‑only plan against dev state shows 5 imported, 2 role assignments added, 3 updated in place (key auth off, tags, the page's settings) and **0 destroyed**. The first plan would have replaced the account, because Foundry's `project_management_enabled` defaults to false and forces replacement, so the module now declares it with the system identity it requires. Not applied yet: applies go through the deploy workflow. Claude stays as a local fallback; the Anthropic account had no credit.
- **First real explanation (UPC_00045):** grounded and cited, with citations ✓ and flags ✓. The number check flagged "approximately 4 versions", a jump the model computed itself (5 − 1), which the prompt forbids. That is the check working. It also flagged the model's own "[Evidence 10]" label, a false positive, now fixed and tested.

**Generation eval (2026‑10‑21), `python -m firmaware.rag eval-generation`.** 18 fixed probes: the riskiest three and the mildest one per vendor, plus the real Moxa deployment and a synthetic out‑of‑corpus vendor. The writer is Llama‑3.3‑70B over Azure AI Search. The judge is gpt‑oss‑120b, a different model family; Mistral‑Large‑3 and DeepSeek had zero quota on the subscription. Gates: citations, numbers, flags and refusal at 100%, judge faithfulness ≥ 0.8.

| Run | Change | Citations | Numbers | Flags | Refusal | Judge faithful |
|---|---|---|---|---|---|---|
| 1 | baseline | 100% | 94% | 100% | 100% | 31% |
| 2 | context gains jump magnitude, maintenance window, past failures, tier; judge told statements of absence are not claims | 100% | 100% | 94% | 100% | 38% |
| 3 | writer: cite a document only for what it says, no unanchored "high/low", no added mechanisms | 100% | 100% | 94%* | 100% | **69%** |

\*Re‑scored after correcting the flag check. It flagged any inactive flag mentioned without a negation, and five of its six hits in run 3 were the writer accurately reporting what the evidence says about past deployments. It now requires the sentence to tie the flag to *this* deployment, and the one genuine borrowing ("a major_version_jump, such as the one planned") is still caught. Tests pin both shapes.

**Both gates still fail: flags (one real borrowing) and judge faithfulness (69% against 80%).** The remaining judge objections are mostly real: Llama compares this deployment's jump with a historical average as if that average were a threshold, and adds consequences the evidence does not state. Two are the judge ignoring its own instruction that a statement of absence is not a claim. The deterministic checks hold the hard line; the judge rate is the quality bar the writer has not yet reached. Ways to close it, untried: a stronger writer model (quota permitting), fewer evidence chunks per prompt, or a second writer pass that removes sentences the judge marks unsupported.

**Measuring the judge before steering by it (2026‑10‑21).** A fourth run, which bundled five changes (writer and judge temperature, the judge prompt, two writer rules), fell to 44%, so the all‑or‑nothing verdict was itself put under test. Saved texts were re‑graded with no writer calls (`eval-generation --rejudge <report> --repeat 3`), using a claim‑level judge that labels every claim `supported` / `unsupported` / `not_a_claim`; code, not the judge, applies the rule that a statement of absence is not a claim.

| Texts | Claim precision, 3 judge passes | All claims supported, 3 passes |
|---|---|---|
| run 3 (writer settings now in use) | 0.958 / 0.952 / 0.946 (spread 0.012) | 75% / 81% / 69% |
| run 4 | 0.929 / 0.933 / 0.916 (spread 0.018) | 56% / 75% / 56% |

So the all‑or‑nothing rate moves 12 points on identical text, and most of the 31 → 69 → 44 swings were judge noise. Claim precision is stable to about a point and a half, and it shows run 4's bundle was genuinely worse, so the writer is back on run 3's settings. **The gate now measures claim precision, provisionally ≥ 0.8; the user sets the real bar.** Run 3's writer measures about 0.95: roughly one claim in twenty is not supported by its evidence.

Two fixes from the same review. A `no_flag_evidence` explanation may cite nothing, though any citation it makes must still be retrieved. And the one borrowed flag came from the context's version‑jump *magnitude* (4.42) being read as a major‑version jump (4.0.26 → 4.2.8); labelling that line is the next single change to measure.

The original P4 design, unchanged where the points above do not override it:
Azure OpenAI over the private endpoint (the `Generator` interface takes an Azure OpenAI implementation under `deploy/azure/rag/`). Prompt: persona, deployment context block, numbered evidence, hard rules — ground every claim, cite `FG-DOC-*`, **never state a number absent from the input**, never attribute a historical flag to the current deployment, end with one concrete action. Gates before any generation call: vendor match (refuse‑to‑fabricate), then the confidence gate. Provenance weighting when summarising agreement with the ML score, because postmortems distil the same records the model trained on and are therefore partially circular confirmation; external advisories are the independent slice.
**Done when:** four deterministic checks pass on a fixed probe set — citation validity, number grounding, flag attribution, refusal correctness — plus an LLM‑judge faithfulness rubric.

### P5 — Serve, wire CI/CD, rehearse rollback
`firmaware-explain-{env}` as its own Managed Online Endpoint, blue/green like the scorer. Explanations cached against the deployment id and index version; cache invalidates on alias repoint. Workflows mirroring `azure-ci` and the deploy pair, with the same blocking security gates.
**Done when:** all four rollback classes rehearsed and timed — alias repoint, endpoint traffic flip, image digest, infra revert.

---

## 5. Sequencing and cost

P0–P1 need no Azure resources at all and are worth doing first regardless of anything else: chunking correctness is the highest‑leverage part of the whole system and is fully testable offline. P2 is the first spend. P3 is where the project learns whether the corpus supports retrieval at all — and if recall@5 is poor on a type slice, the fix is in P1's chunking, not in the model, which is why the phase order matters.

Running estimate per environment: Search Basic ≈ $75/month, Azure OpenAI usage‑based (small at this query volume), explain endpoint ≈ the same as the scoring endpoint. Materially above the ML layer's floor — decide whether dev shares prod's Search service before P2 rather than after.

---

## 6. Open questions before implementation

1. **Embedding path** — integrated vectorization versus pipeline‑computed BGE‑small (§2). Recommend deciding by measurement in P3.
2. **Shared versus per‑environment Search service** — cost against isolation.
3. **Semantic ranker tier** — worth its cost only if P3's baseline shows ranking, not recall, is the bottleneck.
4. **Cache invalidation on model version change** — an explanation cites evidence for a specific risk score; when the *model* is rolled back, are cached explanations still valid? Current answer: invalidate on either model version or index alias change, which is conservative and cheap.

Next artefact, when this plan is agreed: an implementation spec in the same shape as `docs/design/azure-implementation-spec.md` — IaC, scripts, CI/CD, and security controls stated as acceptance tests.
