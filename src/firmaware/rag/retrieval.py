"""Find evidence for one deployment, routed by the question each document answers.

One "why is this risky" query only ever surfaces the documents that answer
why: measured on the corpus, runbooks, transition summaries and cross-vendor
reports scored recall@5 of zero, sitting at median rank 25-52 behind risk
notes and advisories. So each document type is reached by its own route:

    route         documents                           how
    evidence      Postmortem, Advisory, RiskNote      vector search, vendor filter
    transition    Transition                          vendor + jump band, then vector
    cross_vendor  CrossVendor                         primary vendor, then vector
    runbook       Runbook                             vendor + device family (vectorless)
    policy        Policy                              site criticality, then active-flag
                                                      keywords (vectorless)
    matrix        Matrix                              vendor named in it (vectorless)

A route that has no matching document returns nothing. Substituting a
neighbouring vendor's or device's document is the similarity-is-not-relevance
failure the out-of-corpus vendor control exists to catch.

The backend is a filtered search, the shape Azure AI Search exposes (a query,
an OData filter, top-k), so a deploy-side implementation can replace the local
one without the routes changing.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pandas as pd

from .flags import mentions

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EVIDENCE_TYPES = frozenset({"Postmortem", "Advisory", "RiskNote"})
ROUTE_LIMITS = {
    "evidence": 5,
    "transition": 2,
    "cross_vendor": 2,
    "runbook": 2,
    "policy": 2,
    "matrix": 1,
}

Embedder = Callable[[Sequence[str]], np.ndarray]


@dataclass(frozen=True)
class Hit:
    chunk: dict[str, Any]
    score: float  # the backend's own score; cosine locally, not comparable across backends
    route: str


class SearchBackend(Protocol):
    def search(self, query: str, doc_ids: frozenset[str], k: int) -> list[tuple[dict, float]]:
        """Top-k chunks by relevance, restricted to the given documents."""
        ...

    def chunks_of(self, doc_id: str) -> list[dict]:
        """A document's chunks in document order, with no embedding call."""
        ...


class LocalIndex:
    """Exact cosine search over normalized vectors: what FAISS IndexFlatIP returns."""

    def __init__(self, chunks: list[dict], embed: Embedder) -> None:
        self.chunks = chunks
        self._embed = embed
        self._vectors = np.asarray(embed([c["text"] for c in chunks]), dtype=np.float32)
        self._by_doc: dict[str, list[int]] = {}
        for index, chunk in enumerate(chunks):
            self._by_doc.setdefault(chunk["doc_id"], []).append(index)
        self._query: tuple[str, np.ndarray] | None = None

    def _query_vector(self, query: str) -> np.ndarray:
        # One embedding per query, however many vector routes use it.
        if self._query is None or self._query[0] != query:
            self._query = (query, np.asarray(self._embed([query]), dtype=np.float32)[0])
        return self._query[1]

    def search(self, query: str, doc_ids: frozenset[str], k: int) -> list[tuple[dict, float]]:
        candidates = [i for doc_id in doc_ids for i in self._by_doc.get(doc_id, [])]
        if not candidates or k <= 0:
            return []
        scores = self._vectors[candidates] @ self._query_vector(query)
        order = np.argsort(-scores, kind="stable")[:k]
        return [(self.chunks[candidates[i]], float(scores[i])) for i in order]

    def chunks_of(self, doc_id: str) -> list[dict]:
        return [self.chunks[i] for i in self._by_doc.get(doc_id, [])]


def bge_embedder(model_name: str = EMBEDDING_MODEL) -> Embedder:
    """BGE-small, normalized, batch 32: the lecture embedder.

    Imported here, not at module level: it pulls in torch, which only the
    notebook extra installs, so CI and the corpus commands never load it.
    """
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    return lambda texts: model.encode(list(texts), batch_size=32, normalize_embeddings=True)


def build_query(row: pd.Series, flags: list[str]) -> str:
    """The lecture's rich query, minus every identifier.

    The corpus prose cites DEP_* ids, so an id in the query would match on the
    id rather than on the pattern the document describes.
    """
    return (
        f"historical failures for {row['vendor_name']} {row['device_type']} "
        f"firmware upgrade {row['current_firmware']} to {row['target_firmware']} "
        f"risk flags: {' '.join(flags) if flags else 'no major flags'} "
        f"CVSS score {row['max_cvss_score']} "
        f"site criticality {row['site_criticality']} "
        f"deployment type {row['deployment_type']}"
    )


def _docs(records: list[dict], document_type: str, **where: Callable[[dict], bool]) -> frozenset[str]:
    return frozenset(
        r["doc_id"]
        for r in records
        if r["document_type"] == document_type and all(test(r) for test in where.values())
    )


def retrieve(
    row: pd.Series,
    flags: list[str],
    records: list[dict],
    backend: SearchBackend,
) -> dict[str, list[Hit]]:
    """Evidence for one deployment, by route. `row` carries the flags frame's columns."""
    vendor = str(row["vendor_name"])
    band = row["transition_class"]
    band = band if isinstance(band, str) else None
    dependencies = float(row["cross_vendor_dependency_count"])
    has_dependencies = not math.isnan(dependencies) and dependencies > 0
    query = build_query(row, flags)
    routes: dict[str, list[Hit]] = {}

    def vector(route: str, doc_ids: frozenset[str]) -> None:
        found = backend.search(query, doc_ids, ROUTE_LIMITS[route]) if doc_ids else []
        routes[route] = [Hit(chunk, score, route) for chunk, score in found]

    vector(
        "evidence",
        frozenset(
            r["doc_id"]
            for r in records
            if r["document_type"] in EVIDENCE_TYPES and vendor in r["vendors"]
        ),
    )
    vector(
        "transition",
        _docs(
            records,
            "Transition",
            vendor=lambda r: vendor in r["vendors"],
            band=lambda r: r["transition_class"] == band,
        )
        if band
        else frozenset(),
    )
    # The scoring contract says how many cross-vendor dependencies a device has,
    # never with which vendor, and 89% of historical deployments have one. So
    # this route returns the vendor's own cascade reports and cannot pick the
    # pair; it is reported, not gated.
    vector(
        "cross_vendor",
        _docs(records, "CrossVendor", primary=lambda r: r["vendors"][0] == vendor)
        if has_dependencies
        else frozenset(),
    )

    runbooks = _docs(
        records,
        "Runbook",
        vendor=lambda r: vendor in r["vendors"],
        family=lambda r: r["device_family"] == row["device_type"],
    )
    routes["runbook"] = [
        Hit(chunk, 1.0, "runbook")
        for doc_id in sorted(runbooks)
        for chunk in backend.chunks_of(doc_id)[: ROUTE_LIMITS["runbook"]]
    ]

    # Exact match on the site criticality each policy declares, as its own text
    # says ("covers all sites categorized under a MEDIUM criticality profile").
    # A HIGH-only policy's rules otherwise reach MEDIUM sites on keywords alone;
    # a site no policy covers gets none, not the nearest one.
    policies = _docs(
        records,
        "Policy",
        scope=lambda r: r["site_criticality_scope"] == row["site_criticality"],
    )
    policy_chunks = [
        chunk
        for doc_id in sorted(policies)
        for chunk in backend.chunks_of(doc_id)
        if chunk["chunk_kind"] in {"rule", "whole"}
    ]
    # Rule chunks first. A policy PDF that lost its structure is one whole-
    # document chunk naming every flag, so ranked purely on flags matched it
    # would beat every single rule; it only fills slots no rule matched.
    scored = [(sum(mentions(chunk["text"], f) for f in flags), chunk) for chunk in policy_chunks]
    ranked = sorted(
        ((matched, chunk) for matched, chunk in scored if matched),
        key=lambda pair: (pair[1]["chunk_kind"] != "rule", -pair[0]),
    )
    routes["policy"] = [
        Hit(chunk, float(matched), "policy") for matched, chunk in ranked[: ROUTE_LIMITS["policy"]]
    ]

    routes["matrix"] = [
        Hit(chunk, 1.0, "matrix")
        for doc_id in sorted(_docs(records, "Matrix"))
        for chunk in backend.chunks_of(doc_id)
        if vendor in chunk["text"]
    ][: ROUTE_LIMITS["matrix"]]
    return routes
