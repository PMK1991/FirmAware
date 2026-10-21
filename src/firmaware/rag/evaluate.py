"""Grade retrieval with the corpus's own citations, and gate on what can fail.

Every document cites the historical deployments it was written from, so a
query built from one of those rows has a known right answer. Only citations
whose row matches the document's vendor (and device family, where it names
one) count: several risk notes cite other vendors' rows, and "relevant by
construction" is false for those.

What is gated, and what is only reported:

- The evidence route is vector search, and its recall@5 can genuinely fail,
  so it carries the promotion gates.
- The runbook and transition routes select documents by metadata. Scored on
  their own citations they would pass by construction, so they report how
  often they apply and whether they returned the cited document when they did.
- The cross-vendor route fires on any dependency (89% of deployments) and
  cannot pick the pair, so it is reported and never gated.
- Negative controls are gated at 100%: an out-of-corpus vendor must be refused
  and a flag the corpus never discusses must not produce a concern, both
  without a single model call.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

import pandas as pd

from .explain import explain
from .gate import DEFAULT_GATE, GateConfig
from .generate import Generation, Generator, judge_faithfulness
from .retrieval import EVIDENCE_TYPES, SearchBackend, retrieve

ROUTE_OF = {
    **{t: "evidence" for t in EVIDENCE_TYPES},
    "Transition": "transition",
    "CrossVendor": "cross_vendor",
    "Runbook": "runbook",
}
GATES = {"evidence_recall_at_5": 0.8, "evidence_type_recall_at_5": 0.6}
# Generation gates, from the HLD: the deterministic checks hold on every
# generated explanation and refusal is exactly right both ways. Faithfulness is
# an LLM's judgment, gated on claim precision (supported / judged claims across
# the probe set). 0.8 is PROVISIONAL: it was set for the old all-or-nothing
# verdict and has to be re-set from a measured baseline, with the judge's own
# run-to-run spread in view. The share of explanations with no unsupported claim
# at all is reported beside it, ungated.
GENERATION_GATES = {
    "citations_ok": 1.0,
    "numbers_ok": 1.0,
    "flags_ok": 1.0,
    "refusal_correct": 1.0,
    "judge_claim_precision": 0.8,
}
PROBES_PER_VENDOR = 4
OUT_OF_CORPUS_VENDOR = "OutOfCorpusVendor"


class _MustNotGenerate:
    """A generator that fails the run if a gated-out deployment reaches it."""

    name = "must-not-generate"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system: str, user: str) -> Generation:
        self.calls += 1
        return Generation("", "end_turn")


def gold_queries(records: list[dict], history: pd.DataFrame) -> list[dict[str, Any]]:
    cited_by: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for dep in record["source_deployment_ids"]:
            cited_by[dep].add(record["doc_id"])
    gold = []
    for record in records:
        route = ROUTE_OF.get(record["document_type"])
        if route is None:
            continue
        for dep in record["source_deployment_ids"]:
            if dep not in history.index:
                continue
            row = history.loc[dep]
            if row["vendor_name"] not in record["vendors"]:
                continue
            if record["device_family"] and row["device_type"] != record["device_family"]:
                continue
            gold.append(
                {
                    "doc_id": record["doc_id"],
                    "document_type": record["document_type"],
                    "route": route,
                    "vendor": row["vendor_name"],
                    "deployment_id": dep,
                    "relevant": cited_by[dep],
                }
            )
    return gold


def _rate(values: list[bool]) -> float | None:
    return round(mean(values), 4) if values else None


def evaluate(
    records: list[dict],
    backend: SearchBackend,
    history: pd.DataFrame,
    upcoming: pd.DataFrame,
    config: GateConfig = DEFAULT_GATE,
) -> dict[str, Any]:
    """`history` and `upcoming` are `deployment_flags` frames."""
    gold = gold_queries(records, history)
    outcomes: dict[str, list[dict]] = defaultdict(list)
    for query in gold:
        row = history.loc[query["deployment_id"]]
        hits = retrieve(row, list(row["active_flags"]), records, backend)[query["route"]]
        returned = [hit.chunk["doc_id"] for hit in hits]
        rank = returned.index(query["doc_id"]) + 1 if query["doc_id"] in returned else None
        outcomes[query["route"]].append({**query, "applied": bool(hits), "rank": rank})

    evidence = outcomes["evidence"]
    report: dict[str, Any] = {
        "gold_queries": len(gold),
        "evidence": {
            "queries": len(evidence),
            "recall_at_5": _rate([o["rank"] is not None for o in evidence]),
            "mrr": round(mean(1 / o["rank"] if o["rank"] else 0.0 for o in evidence), 4)
            if evidence
            else None,
            "recall_by_type": {
                t: _rate([o["rank"] is not None for o in evidence if o["document_type"] == t])
                for t in sorted({o["document_type"] for o in evidence})
            },
            "recall_by_vendor": {
                v: _rate([o["rank"] is not None for o in evidence if o["vendor"] == v])
                for v in sorted({o["vendor"] for o in evidence})
            },
        },
        "metadata_routes": {
            route: {
                "queries": len(outcomes[route]),
                "applied": _rate([o["applied"] for o in outcomes[route]]),
                "returned_cited_doc_when_applied": _rate(
                    [o["rank"] is not None for o in outcomes[route] if o["applied"]]
                ),
            }
            for route in ("transition", "cross_vendor", "runbook")
        },
    }

    verdicts: list[dict] = []
    coverage: dict[str, list[bool]] = defaultdict(list)
    for dep, row in upcoming.iterrows():
        routes = retrieve(row, list(row["active_flags"]), records, backend)
        for route, hits in routes.items():
            coverage[route].append(bool(hits))
        result = explain(row, records, backend, None, config)
        verdicts.append({"deployment_id": dep, "band": row.get("risk_band"),
                         "verdict": result.assessment.verdict})
    report["upcoming"] = {
        "deployments": len(verdicts),
        "route_coverage": {route: _rate(values) for route, values in coverage.items()},
        "verdicts": pd.Series([v["verdict"] for v in verdicts]).value_counts().to_dict(),
        "verdicts_by_band": pd.crosstab(
            pd.Series([v["band"] for v in verdicts], name="band").fillna("unscored"),
            pd.Series([v["verdict"] for v in verdicts], name="verdict"),
        ).to_dict(orient="index"),
    }
    report["negative_controls"] = _negative_controls(records, backend, upcoming, config)

    gates = {
        "evidence_recall_at_5": (report["evidence"]["recall_at_5"] or 0) >= GATES["evidence_recall_at_5"],
        **{
            f"evidence_recall_at_5[{t}]": (r or 0) >= GATES["evidence_type_recall_at_5"]
            for t, r in report["evidence"]["recall_by_type"].items()
        },
        "negative_controls": all(c["passed"] for c in report["negative_controls"]),
    }
    report["gates"] = gates
    report["passed"] = all(gates.values())
    return report


def _negative_controls(
    records: list[dict], backend: SearchBackend, upcoming: pd.DataFrame, config: GateConfig
) -> list[dict[str, Any]]:
    covered = sorted({vendor for r in records for vendor in r["vendors"]})
    in_corpus = upcoming[upcoming["vendor_name"].isin(covered)]
    # (name, row, expected verdict, reason the verdict must give). The reason
    # matters: thin evidence would also produce low_confidence, and a control
    # that passes for the wrong cause proves nothing.
    probes: list[tuple[str, pd.Series, str, str]] = []
    for dep, row in upcoming[~upcoming["vendor_name"].isin(covered)].iterrows():
        probes.append((f"real out-of-corpus vendor {row['vendor_name']} ({dep})", row,
                       "insufficient_evidence", "evidence in the corpus"))
    if len(in_corpus):
        synthetic = in_corpus.iloc[0].copy()
        synthetic["vendor_name"] = OUT_OF_CORPUS_VENDOR
        probes.append(("synthetic out-of-corpus vendor", synthetic, "insufficient_evidence",
                       "evidence in the corpus"))
        ungrounded = in_corpus.iloc[0].copy()
        ungrounded["active_flags"] = ["high_network_stress"]  # no document discusses it
        probes.append(("flag the corpus never discusses", ungrounded, "low_confidence",
                       "active flags grounded"))

    controls = []
    for name, row, expected, reason in probes:
        sentinel = _MustNotGenerate()
        assessment = explain(row, records, backend, sentinel, config).assessment
        for_the_right_reason = any(reason in r for r in assessment.reasons)
        controls.append({"probe": name, "expected": expected, "verdict": assessment.verdict,
                         "reasons": assessment.reasons, "generator_calls": sentinel.calls,
                         "passed": assessment.verdict == expected and for_the_right_reason
                         and sentinel.calls == 0})
    return controls


def generation_probes(
    records: list[dict], upcoming: pd.DataFrame, per_vendor: int = PROBES_PER_VENDOR
) -> list[tuple[str, pd.Series]]:
    """A fixed probe set: the riskiest and the mildest of each vendor, plus refusals.

    Deterministic (ordered by score, then id) so a rerun grades the same
    deployments and a change in the numbers is a change in the system.
    """
    covered = sorted({vendor for r in records for vendor in r["vendors"]})
    ranked = upcoming.assign(_p=upcoming.get("risk_probability", 0.0)).sort_values(
        ["_p"], ascending=False, kind="stable"
    )
    probes: list[tuple[str, pd.Series]] = []
    for vendor in covered:
        rows = ranked[ranked["vendor_name"] == vendor]
        picked = list(rows.index[: per_vendor - 1]) + list(rows.index[-1:])
        for dep in dict.fromkeys(picked):  # dedupe, keep order
            probes.append((f"{vendor} {dep}", rows.loc[dep].drop("_p")))
    for dep, row in upcoming[~upcoming["vendor_name"].isin(covered)].iterrows():
        probes.append((f"out-of-corpus {row['vendor_name']} {dep}", row))
    if len(probes) and len(upcoming[upcoming["vendor_name"].isin(covered)]):
        synthetic = upcoming[upcoming["vendor_name"].isin(covered)].iloc[0].copy()
        synthetic["vendor_name"] = OUT_OF_CORPUS_VENDOR
        probes.append(("synthetic out-of-corpus vendor", synthetic))
    return probes


def evaluate_generation(
    records: list[dict],
    backend: SearchBackend,
    upcoming: pd.DataFrame,
    generator: Generator,
    judge: Generator | None = None,
    config: GateConfig = DEFAULT_GATE,
) -> dict[str, Any]:
    """Explain a fixed probe set with the real generator and grade every answer.

    Per explanation: citations only to retrieved documents, every number
    present in the input, no borrowed flag, and an LLM judge's faithfulness
    verdict. Per probe: refusal is right both ways, so an out-of-corpus vendor
    is refused with no model call and an in-corpus one is never refused.
    """
    covered = {vendor for r in records for vendor in r["vendors"]}
    rows = []
    for name, row in generation_probes(records, upcoming):
        result = explain(row, records, backend, generator, config)
        out_of_corpus = row["vendor_name"] not in covered
        refused = result.assessment.verdict == "insufficient_evidence"
        entry: dict[str, Any] = {
            "probe": name,
            "deployment_id": str(row.name),
            "vendor": row["vendor_name"],
            "band": row.get("risk_band"),
            "verdict": result.assessment.verdict,
            "refusal_correct": refused == out_of_corpus
            and (not out_of_corpus or result.generated_by is None),
            "generated": result.generated_by is not None,
            "error": result.error,
            "text": result.text,
            # Kept so a later judge can re-grade this exact text against this
            # exact input without a single writer call.
            "context": result.context,
            "evidence_ids": [hit.chunk["chunk_id"] for hit in result.evidence],
        }
        if result.generated_by:
            entry.update({k: result.checks[k] for k in
                          ("citations_ok", "numbers_ok", "flags_ok", "unretrieved_citations",
                           "stray_numbers", "borrowed_flags")})
            if judge is not None:
                entry["judge"] = judge_faithfulness(judge, result.context, result.evidence,
                                                    result.text or "")
        rows.append(entry)

    generated = [r for r in rows if r["generated"]]
    rates = {
        "citations_ok": _rate([r["citations_ok"] for r in generated]),
        "numbers_ok": _rate([r["numbers_ok"] for r in generated]),
        "flags_ok": _rate([r["flags_ok"] for r in generated]),
        "refusal_correct": _rate([r["refusal_correct"] for r in rows]),
        **_judge_rates([r.get("judge") for r in generated]),
    }
    gates = {
        name: rates[name] is not None and rates[name] >= floor
        for name, floor in GENERATION_GATES.items()
        if not (name.startswith("judge_") and judge is None)
    }
    gates["no_generation_errors"] = not any(r["error"] for r in rows)
    return {
        "generator": getattr(generator, "name", None),
        "judge": getattr(judge, "name", None),
        "probes": len(rows),
        "generated": len(generated),
        "verdicts": pd.Series([r["verdict"] for r in rows]).value_counts().to_dict(),
        "rates": rates,
        "gates": gates,
        "passed": all(gates.values()),
        "results": rows,
    }


def _judge_rates(verdicts: list[dict | None]) -> dict[str, float | None]:
    judged = [v for v in verdicts if v and v.get("precision") is not None]
    supported = sum(v["supported"] for v in judged)
    unsupported = sum(len(v["unsupported_claims"]) for v in judged)
    return {
        "judge_claim_precision": round(supported / (supported + unsupported), 4)
        if supported + unsupported else None,
        "judge_all_supported": _rate([v["faithful"] for v in judged]),
        "judge_unparseable": sum(1 for v in verdicts if v and v.get("precision") is None),
    }


def rejudge(
    report: dict[str, Any],
    records: list[dict],
    backend: SearchBackend,
    upcoming: pd.DataFrame,
    judge: Generator,
    repeat: int = 3,
    config: GateConfig = DEFAULT_GATE,
) -> dict[str, Any]:
    """Re-grade a saved run's explanations with the current judge, `repeat` times.

    No writer calls: the texts are the saved ones, and each probe's context and
    evidence are rebuilt by retrieval alone. Two questions this answers that a
    fresh run cannot: did a score move because the writer changed or because
    the judge did, and how far does the judge move on identical text.
    """
    rows = []
    for saved in report["results"]:
        if not saved.get("generated") or saved["deployment_id"] not in upcoming.index:
            continue
        retrieved = explain(upcoming.loc[saved["deployment_id"]], records, backend, None, config)
        context = saved.get("context") or retrieved.context
        verdicts = [judge_faithfulness(judge, context, retrieved.evidence, saved["text"])
                    for _ in range(repeat)]
        rows.append({"probe": saved["probe"], "verdicts": verdicts})
    passes = [_judge_rates([row["verdicts"][i] for row in rows]) for i in range(repeat)]
    precisions = [p["judge_claim_precision"] for p in passes if p["judge_claim_precision"]]
    return {
        "judge": getattr(judge, "name", None),
        "explanations": len(rows),
        "passes": passes,
        "precision_mean": round(mean(precisions), 4) if precisions else None,
        "precision_spread": round(max(precisions) - min(precisions), 4) if precisions else None,
        "results": rows,
    }
