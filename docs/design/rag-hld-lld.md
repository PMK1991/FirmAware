# FirmAware RAG — HLD + LLD

> Adds the evidence layer to FirmAware. Companion to `FirmAware_ML_Pipeline_Spec.md` (the prediction layer) and `FirmAware_GCP_Deployment_Spec.md` (ops). Best practices are lifted from the FirmGuard course lectures (S12–S15, S19, bonus NLP sessions) — each carried practice is traceable to its source in §0.
> Version 1.0 · 2026‑07‑22 · Corpus: the 69‑document FirmGuard knowledge base (usable as‑is; synthetic but numerically grounded in the 20k deployment records).

---

## 0. Best practices carried from the lectures (traceability)

| Practice | Source |
|---|---|
| Document‑aware chunking — one strategy per type, never one generic 512‑token splitter | S15 `02_chunker.docx`; META KB §5 ("document‑aware chunking is not optional") |
| Chunks carry hard metadata (type, vendor, retrieval mode) — trust checks need facts, not similarity | S15 chunker; S17 specialist stage |
| Cosine = dot product over normalized vectors → FAISS `IndexFlatIP` | S13 (taught from first principles); S15 `04_ingestor.docx` |
| Local embeddings + local index at this scale — no hosted vector DB, no API latency/cost | S19 `FirmGuard rag.docx` ("no quality benefit at this scale") |
| Rich query construction — feed the embedder the full situation, not a bare topic | S15 `05_retriever.docx` (worked example) |
| Retriever stays filter‑free; consumers over‑fetch then filter on metadata | S15 retriever doc; S17 `rag_specialist` (×4 over‑fetch) |
| Vector‑search dilution is fought with metadata filtering + reranking as the corpus grows | S15 retriever doc ("production retrieval pipeline") |
| Backup index before every ingest; timestamped restore | S15 `07_pipeline.docx`; S19 rag doc |
| Refuse‑to‑fabricate: zero vendor‑matched evidence ⇒ never call the generator | S16 `soloist.py` |
| Deterministic confidence gate before any "no concern" (match count, similarity, doc spread, flag grounding) | S17 `rag_specialist.assess_confidence` |
| Ground every claim; cite doc IDs; no numbers absent from input; end with an action | S15 `06_generator.docx`; S17 orchestrator defect log |
| PDF extraction loses structure (headings/tables) — Markdown is the canonical authoring format; Document AI is the upgrade path | S19 rag doc (observed: PDF docs collapsed to 1 chunk vs 9–10) |
| Cache generated explanations; never regenerate on view | S19 rag doc (`cached_explanation`) |
| Evidence strength rating + source‑record traceability on every document | META KB §7 |
| Embedding ladder (BOW→TF‑IDF→Word2Vec→transformer) justifies dense embeddings; BGE‑M3 1024‑d flagged as the upgrade to evaluate | Bonus NLP sessions; S15 notes |

---

## 1. HLD

### 1.1 Role

The ML layer (AUC ≈ 0.72) is a **triage instrument** — it ranks and routes. The RAG layer answers the question the classifier cannot: *why* is this deployment risky, with citable historical evidence. Output consumers: the scored‑deployments review flow (per‑deployment "explain" with citations) and, later, any agent/checker tier built on top.

### 1.2 Architecture

```
OFFLINE (ingest — rerun only when corpus changes)
  corpus bucket (md canonical, pdf tolerated)
    → parser      (normalize to md; record source_format)
    → chunker     (9 typed strategies → ~409 chunks + metadata)
    → embedder    (BGE-small-en-v1.5, 384-d, normalized, batch 32)
    → indexer     (FAISS IndexFlatIP + metadata sidecar, same order)
    → versioned index artifact + automatic pre-ingest backup
    → eval harness (§7) runs BEFORE the new index is promoted

ONLINE (per deployment)
  scored row → build_query (rich: vendor, device, firmware jump, flags,
               CVSS, criticality, type)
    → retrieve top-k (over-fetch ×4 → metadata filter → k=5)
    → confidence gate (deterministic; 4 checks)
        ├─ pass  → generator (grounded, cited, no un-sourced numbers) → cache
        └─ fail  → honest verdict: low_confidence | insufficient_evidence
                   (never a confident-sounding answer)
```

### 1.3 Storage & deployment (extends the GCP spec)

One new bucket `firmaware-{env}-corpus` (versioned; `raw/`, `parsed/`, `index/runs/{run_id}/`, `index/champion.json`); one new Cloud Run Job `firmaware-rag-ingest` (manual trigger — corpus changes are deliberate acts, like training). The **champion‑pointer pattern from the deployment spec is reused verbatim** for the index: ingest writes a new `index/runs/{run_id}/`, eval gates promotion, `champion.json` repoints; rollback = repoint (< 2 min). Embedding + FAISS run in‑process — no new services, no secrets.

### 1.4 Non‑goals (this phase)

Graph RAG (cross‑vendor docs are staged for it; later), reranker model (upgrade path defined, not built), hosted vector DB, LLM‑based chunking, agent tier.

---

## 2. LLD — Corpus: document types

Nine types. Each answers a **different** retrieval question — by design, so retrieval doesn't return redundant results. Counts from the S19 corpus.

| Document type | Count | Style (deliberate heterogeneity) | Question answered | Retrieval mode |
|---|---|---|---|---|
| Postmortem report | 13 | Reliability‑engineer: formal, narrative, past‑tense | What happened in similar past failures? | vector |
| Vendor advisory | 12 | Vendor‑analyst: warning tone, structured, imperative | What risk patterns does this vendor show? | hybrid |
| Risk investigation note | 12 | Analyst: dense evidence‑based prose | Why does this flag combination create risk? | vector |
| Rollback runbook | 8 | Operations: numbered, action‑verb‑led | What do I do when this fails? | vectorless |
| Deployment policy | 5 | Rule text with exceptions | What are the rules for this scenario? | vectorless |
| Classification matrix | 4 | Near‑zero prose, pure table | Risk profile of this vendor/tier? | vectorless |
| Firmware transition summary | 8 | Mixed structured + narrative | What goes wrong in this version‑jump range? | hybrid |
| Cross‑vendor pattern report | 6 | Analyst prose, relationship‑oriented | What cascade risks between vendor families? | vector |
| META knowledge‑base doc | 1 | Reference | What is this corpus? | always retrievable |

**Per‑document required front‑matter (metadata contract):** `doc_id (FG-DOC-\d+)`, `document_type`, `vendor` (or `cross‑vendor`), `evidence_strength {HIGH≥100, MEDIUM 20–99, LOW 5–19 source records}`, `source_deployment_ids [DEP_*]`, `provenance {internal_derived | external_sourced}`. The last two power eval (§7) and provenance‑weighted agreement respectively. Corpus references **only** historical `DEP_*` ids — never upcoming `UPC_*` (enforced by a lint in ingest).

---

## 3. LLD — Splitting & chunking (per type)

**The key insight (S15):** one generic splitter destroys the structure each type deliberately carries. A halved table is meaningless; an isolated runbook step is unusable; a rule split from its exception clause is *misleading* — worse than nothing.

| Document type | Split strategy | Chunk kind | Invariant enforced by tests |
|---|---|---|---|
| Postmortem | by `##` section heading | `prose` | every chunk begins at a heading; sections (Root Cause, Lessons Learned) never merged |
| Vendor advisory | tables atomic; prose by paragraph, heading prepended | `table` / `prose` | no `\|` table row separated from its header row |
| Risk investigation note | paragraph groups, 300–700 tokens (`words × 1.3`) | `prose` | 300 ≤ tokens ≤ 700 for every chunk |
| Rollback runbook | numbered steps, groups of 4 | `step` | each chunk ≥2 consecutive steps; numbering contiguous within a chunk |
| Deployment policy | one numbered rule per chunk, heading context prepended | `rule` | exactly one rule id per chunk; exception clauses stay with their rule |
| Classification matrix | whole document, no split | `whole` | 1 chunk per matrix doc |
| Firmware transition summary | by `##` heading | `prose` | as postmortem |
| Cross‑vendor pattern report | by `##` heading (one relationship pair per section) | `prose` | one vendor‑pair per chunk |
| META | whole document | `whole` | 1 chunk |

**Chunk record (exact shape, carried end to end):**
```python
{"chunk_id": "FG-DOC-001_chunk_2", "doc_id": "FG-DOC-001", "chunk_index": 2,
 "chunk_kind": "prose", "document_type": "Postmortem", "retrieval_mode": "vector",
 "vendor": "Siemens", "evidence_strength": "HIGH", "provenance": "internal_derived",
 "source_format": "md", "filename": "...", "text": "..."}
```

**Parsing rules:** Markdown is canonical; PDFs pass through `pdfplumber` with the **known structural‑loss caveat** — a PDF advisory can collapse to one chunk. Mitigation now: prefer authoring/converting to md; flag any doc whose chunk count deviates >50% from its type's median (ingest warning). Upgrade path: Document AI Layout Parser. New types are added by registering a strategy in the chunker dispatch dict — the taxonomy is open, the dispatch pattern is fixed.

---

## 4. LLD — Embedding & index

`BAAI/bge-small-en-v1.5`, 384‑d, normalized, batch 32, lazy‑loaded. Similarity = cosine = dot product on normalized vectors → FAISS **`IndexFlatIP(384)`** — exact search, no ANN approximation needed at ~409 chunks. Index + metadata JSON persisted in identical order (position *is* the join key). All local/in‑process: at this corpus size a hosted vector DB adds latency and cost with no quality benefit. **Flagged evaluation (not default): BGE‑M3 1024‑d** — adopt only if §7's retrieval metrics show headroom, per "complexity is earned."

---

## 5. LLD — Retrieval

1. **Rich query construction** (biggest single retrieval lever): concatenate vendor, device type, firmware from→to + jump, active risk flags, CVSS, site criticality, deployment type — never a bare "failures for {vendor}" string.
2. **Over‑fetch ×4, then metadata filter, then truncate to k=5.** The retriever itself stays filter‑free (single responsibility); consumers filter on the metadata they care about (type exclusions, vendor).
3. **Mode‑aware routing (the hybrid router, wired this time):** `retrieval_mode` is already on every chunk — route by *question kind*: semantic questions → vector search; vendor/version‑scoped questions → vector + keyword boost on metadata (hybrid); rule/table lookups (policy, matrix, runbook by device type) → **no embedding at all**, direct metadata/keyword select (vectorless). Router is plain Python on query features, not a model call.
4. **Reranking**: defined upgrade path (cross‑encoder over top‑20) — deferred until dilution is *measured* (§7 catches it), not anticipated.
5. Caching: index + model held in memory across requests; generated explanations cached with the scored row, keyed by deployment + index run_id (stale cache invalidates on index promotion).

---

## 6. LLD — Generation & grounding

Prompt = persona (OT risk analyst) + deployment context block + numbered evidence chunks + rules. Hard rules, all defect‑derived: ground every claim in a numbered chunk; cite `FG-DOC-*` inline; **never state a number absent from the input**; never attribute a flag from historical evidence to the current deployment unless it's in the context block's active‑flag list; end with one concrete action.

**Gates before generation (in order):**
- **Vendor match** (refuse‑to‑fabricate): zero vendor‑matched chunks ⇒ return `insufficient_evidence` with an explicit "do not treat similarity as relevance" message. Never generate.
- **Confidence gate** (deterministic, on vendor‑matched chunks only): `≥3` matches; mean similarity `≥0.45`; `≥2` distinct docs; `≥1/3` of active flags appear in evidence text. Any failure ⇒ `low_confidence`, stated as such.
- **Provenance weighting:** when summarizing agreement with the ML score, weight `external_sourced` evidence (advisories, CVE data) above `internal_derived` (postmortems distilled from the same records the model trained on — partially circular confirmation).

---

## 7. Eval strategy

Three levels, all automated, run by the ingest job **before index promotion** (fail ⇒ champion pointer not moved) and by CI on chunker/retriever changes.

### 7.1 Chunking eval (structural)

Assert every invariant in §3's table across the full corpus: no split tables, one rule per chunk, step‑group contiguity, token bounds, heading alignment, exactly‑one‑chunk for matrices/META. Plus regression: chunk count per type within ±10% of the recorded baseline (catches parser drift and the PDF‑collapse failure mode automatically). Zero LLM calls — pure structural tests.

### 7.2 Retrieval eval (the corpus grades itself)

The corpus's traceability design gives a **free gold standard**: every document lists its `source_deployment_ids`. Invert it:

1. For each document D, sample historical deployments from D's source records; build the rich query (§5.1) from each row.
2. Gold relevance: D (and any other doc listing that DEP_ id) is relevant to that query.
3. Metrics, per document type and per vendor: **recall@5, MRR, mean gold‑doc rank.** Slice by type deliberately — the four writing styles mean a retriever can score well on postmortems and fail on runbooks; the aggregate hides it, the slice doesn't.
4. **Negative controls:** (a) out‑of‑corpus vendor probes ("Moxa case") — expected result: zero vendor‑matched chunks, gate returns `insufficient_evidence`; scoring *any* confident answer is a test failure. (b) shuffled‑flag probes — a query whose flags match no document's patterns should land `low_confidence`, not `no_concern`.
5. Promotion gates (initial, tighten after baseline): recall@5 ≥ 0.8 overall and ≥ 0.6 on every type slice; both negative controls pass 100%.

### 7.3 Generation eval (grounding, not eloquence)

On a fixed probe set (~20 deployments spanning vendors, bands, and both negative controls):
- **Citation validity** (deterministic): every `FG-DOC-*` cited exists in the retrieved set; ≥1 citation per claim‑bearing sentence; zero citations to non‑retrieved docs.
- **Number grounding** (deterministic): every numeral in the output appears in the evidence or context block — the "1,300+ cases" fabrication class, caught mechanically.
- **Flag attribution** (deterministic): no flag named as active unless in the context block.
- **Faithfulness** (LLM‑judge, tool‑forced rubric, pass/fail + reason): does each claim follow from its cited chunk? Judge model ≠ generator model where possible; judge verdicts are gates in CI but always human‑spot‑checked on failure.
- **Refusal correctness:** OOD probes must produce refusals; in‑corpus probes must not (over‑refusal is also a failure).

### 7.4 Reporting

Each eval run writes `eval/{run_id}/report.json` (all metrics + per‑slice tables) next to the index run; the champion pointer records which eval report approved it. A one‑line summary (recall@5, worst slice, negative‑control status) prints in the ingest log — the §5 dilution problem is detected by *watching the worst slice degrade* as the corpus grows, which is the trigger for building the reranker.

---

## 8. Acceptance criteria

1. Ingest on the 69‑doc corpus yields ~409 chunks; §7.1 structural suite green; per‑type counts match the baseline table.
2. A PDF‑sourced advisory triggers the chunk‑count deviation warning (known collapse case reproduced and flagged).
3. Retrieval gold‑set eval runs from `source_deployment_ids` alone (no hand‑labeled data) and reports per‑type slices.
4. Moxa probe: zero vendor matches → `insufficient_evidence`; no generator call is made (assert via call counter).
5. A rule chunk retrieved for a policy question contains its exception clause (spot test on each policy doc).
6. Vectorless route answers a matrix lookup with **zero** embedding calls.
7. Index promotion blocked when any §7 gate fails; `champion.json` unmoved; restore path verified.
8. Generated explanation on a HIGH‑band deployment passes all four deterministic grounding checks; cached on second view (no second LLM call).
9. Everything runs locally end to end (no cloud dependency for dev), and in the `firmaware-rag-ingest` job on GCP with the same results.
