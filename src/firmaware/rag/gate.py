"""Decide, deterministically, whether the evidence can support an explanation.

The S17 confidence gate, on the evidence route's vendor-matched chunks only. A
policy or matrix is relevant to every vendor, so it is never evidence that this
vendor is covered, and policy chunks name nearly every flag, so counting them
toward flag grounding made that check pass for almost any deployment.

    insufficient_evidence  no vendor-matched evidence: refuse, never generate
    low_confidence         a check failed: report why, never generate
    concern_found          confident, and the evidence discusses the raised flags
    no_flag_evidence       confident, but the deployment raises no flag

The last is not "no concern". The corpus documents only failures: it can
corroborate a concern about a raised flag, it cannot clear a deployment, and a
HIGH score with no flag rests on features the corpus does not discuss.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .flags import mentions
from .retrieval import Hit

VERDICTS = ("insufficient_evidence", "low_confidence", "concern_found", "no_flag_evidence")


@dataclass(frozen=True)
class GateConfig:
    min_vendor_chunks: int = 3
    min_documents: int = 2
    min_flag_grounding: float = 1 / 3
    # Off by default, and per backend when on. S17 used 0.45 on BGE-small
    # cosines, but measured over the corpus every top-20 hit scores near 0.85
    # (gold 0.859, others 0.845, an out-of-corpus vendor 0.82), so 0.45 never
    # fires. AI Search hybrid returns fused rank scores, not cosines, so no one
    # number carries across backends; calibrate from that backend's eval.
    min_mean_score: float | None = None


DEFAULT_GATE = GateConfig()


@dataclass(frozen=True)
class Assessment:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    matched: int = 0
    documents: int = 0
    mean_score: float | None = None
    grounded: list[str] = field(default_factory=list)


def assess(
    evidence: list[Hit], vendor: str, flags: list[str], config: GateConfig = DEFAULT_GATE
) -> Assessment:
    matched = [hit for hit in evidence if vendor in hit.chunk["vendors"]]
    if not matched:
        return Assessment("insufficient_evidence", [f"no {vendor} evidence in the corpus"])

    documents = len({hit.chunk["doc_id"] for hit in matched})
    mean_score = sum(hit.score for hit in matched) / len(matched)
    text = " ".join(hit.chunk["text"] for hit in matched)
    grounded = [flag for flag in flags if mentions(text, flag)]

    reasons = []
    if len(matched) < config.min_vendor_chunks:
        reasons.append(f"{len(matched)} vendor-matched chunks (need {config.min_vendor_chunks})")
    if documents < config.min_documents:
        reasons.append(f"{documents} distinct document(s) (need {config.min_documents})")
    if config.min_mean_score is not None and mean_score < config.min_mean_score:
        reasons.append(f"mean score {mean_score:.3f} (need {config.min_mean_score})")
    if flags and len(grounded) / len(flags) < config.min_flag_grounding:
        reasons.append(f"{len(grounded)}/{len(flags)} active flags grounded in the evidence")

    if reasons:
        verdict = "low_confidence"
    else:
        verdict = "concern_found" if flags else "no_flag_evidence"
    return Assessment(verdict, reasons, len(matched), documents, round(mean_score, 4), grounded)
