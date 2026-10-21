"""Hold the evidence corpus to a metadata contract before anything indexes it.

The documents carry their metadata inline, in four layouts that differ by type
and by format: a `| Field | Value |` table, `**Field:** value` lines, and the
flattened `Field: value` / `Field value` a PDF extracts to. PDFs cannot carry
front-matter at all, so the contract lives in a committed sidecar manifest and
this module checks that every document still agrees with it.

Every vocabulary here is closed on purpose. A regex over an open value would
happily read "Total Failures" as a vendor out of a matrix header row.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
INDEX_NAME = "corpus_index.csv"

# Filename prefix -> retrieval mode, from the HLD's document-type table. The
# prefixes are corpus_index.csv's own type names, which the lecture chunk
# metadata also uses, so no second vocabulary is introduced.
DOCUMENT_TYPES = {
    "Postmortem": "vector",
    "Advisory": "hybrid",
    "RiskNote": "vector",
    "Runbook": "vectorless",
    "Policy": "vectorless",
    "Matrix": "vectorless",
    "Transition": "hybrid",
    "CrossVendor": "vector",
    "META": "always",
}
VENDORS = ("Cisco", "Honeywell", "Schneider", "Siemens")
DEVICE_FAMILIES = ("PLC", "HMI", "RTU", "Switch", "Gateway", "Controller")
EVIDENCE_STRENGTHS = ("HIGH", "MEDIUM", "LOW")
PROVENANCES = ("internal_derived", "external_sourced")
# Types whose filename names a single vendor and device family.
DEVICE_SCOPED_TYPES = {"Postmortem", "Advisory", "RiskNote", "Runbook"}
# Transition summaries come in two bands of version_jump_magnitude, named in the
# filename (Transition_<vendor>_<class>) and in the body's Transition Category.
TRANSITION_CLASSES = ("major", "extreme")
VENDOR_NEUTRAL = "vendor-neutral"
CROSS_VENDOR = "cross-vendor"
# META describes the corpus rather than any evidence, so it has no id of its
# own. It takes the one slot the numbering leaves free, which keeps the
# FG-DOC-\d+ citation contract total.
META_DOC_ID = "FG-DOC-000"

DOC_ID = re.compile(r"FG-DOC-\d{3}")
DEPLOYMENT_REF = re.compile(r"\bDEP_\d+")
VALID_DEPLOYMENT_ID = re.compile(r"DEP_\d{6}")
# Word-bounded and digit-bearing: META explains the "(UPC_ prefix)" in prose,
# and that sentence is the rule being stated, not a breach of it.
UPCOMING_REF = re.compile(r"\bUPC_\d+")


def _field(text: str, name: str, values: tuple[str, ...]) -> str | None:
    """First value of a named field, in any of the four inline layouts."""
    pattern = rf"{name}[\s|:*]+({'|'.join(values)})\b"
    match = re.search(pattern, text)
    return match.group(1) if match else None


def read_text(path: Path) -> str:
    if path.suffix == ".pdf":
        import pdfplumber  # the optional "rag" extra; only PDFs need it

        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    return path.read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document_files(corpus_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in corpus_dir.iterdir()
        if path.suffix in {".md", ".pdf"}
    )


def _filename_facts(path: Path) -> dict[str, Any]:
    """What the filename asserts: the name is a contract in its own right."""
    parts = path.stem.split("_")
    document_type = parts[0]
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(f"{path.name}: unknown document type {document_type!r}")
    doc_id = META_DOC_ID if document_type == "META" else parts[-1]
    if document_type == "CrossVendor":
        vendors = [parts[1], parts[3]]  # CrossVendor_<a>_to_<b>_FG-DOC-nnn
    elif document_type in {"Policy", "Matrix", "META"}:
        vendors = []
    else:
        vendors = [parts[1]]
    device_family = parts[2] if document_type in DEVICE_SCOPED_TYPES else None
    transition_class = parts[2] if document_type == "Transition" else None
    return {
        "doc_id": doc_id,
        "document_type": document_type,
        "vendors": vendors,
        "device_family": device_family,
        "transition_class": transition_class,
    }


def _vendor_label(vendors: list[str]) -> str:
    if not vendors:
        return VENDOR_NEUTRAL
    if len(vendors) > 1:
        return CROSS_VENDOR
    return vendors[0]


def _deployment_refs(text: str) -> tuple[list[str], list[str]]:
    refs = set(DEPLOYMENT_REF.findall(text))
    valid = sorted(ref for ref in refs if VALID_DEPLOYMENT_ID.fullmatch(ref))
    # Short ids cannot be repaired by padding: DEP_0143 could be DEP_000143 or
    # a truncation of anything beginning DEP_0143. They are kept visible and
    # out of the gold set rather than guessed at.
    malformed = sorted(refs - set(valid))
    return valid, malformed


def describe(path: Path, text: str | None = None) -> dict[str, Any]:
    """Derive one manifest record from a document's name and body."""
    facts = _filename_facts(path)
    text = read_text(path) if text is None else text
    source_ids, malformed = _deployment_refs(text)
    return {
        "doc_id": facts["doc_id"],
        "filename": path.name,
        "source_format": path.suffix.lstrip("."),
        "document_type": facts["document_type"],
        "retrieval_mode": DOCUMENT_TYPES[facts["document_type"]],
        "vendor": _vendor_label(facts["vendors"]),
        "vendors": facts["vendors"],
        "device_family": facts["device_family"],
        "transition_class": facts["transition_class"],
        "evidence_strength": _field(text, "Evidence Strength", EVIDENCE_STRENGTHS),
        # Every document declares itself derived from deployment data, and the
        # corpus was generated by a local model over the same records the risk
        # model trains on. There is no independent slice yet; labelling the
        # advisories external would invent one.
        "provenance": "internal_derived",
        "source_deployment_ids": source_ids,
        "malformed_refs": malformed,
        "sha256": sha256(path),
    }


REQUIRED_FIELDS = frozenset(
    {
        "doc_id", "filename", "source_format", "document_type", "retrieval_mode",
        "vendor", "vendors", "device_family", "transition_class",
        "evidence_strength", "provenance",
        "source_deployment_ids", "malformed_refs", "sha256",
    }
)


def build_manifest(corpus_dir: Path, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "documents": [describe(path) for path in document_files(corpus_dir)],
    }


def load_manifest(corpus_dir: Path) -> dict[str, Any]:
    return json.loads((corpus_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def write_manifest(corpus_dir: Path, manifest: dict[str, Any]) -> None:
    # LF on every platform, so regenerating on Windows does not churn it.
    (corpus_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _record_problems(record: dict[str, Any], path: Path) -> list[str]:
    name = record["filename"]
    missing = sorted(REQUIRED_FIELDS - set(record))
    if missing:
        return [f"{name}: manifest record is missing {missing}"]
    problems = []
    if record["document_type"] not in DOCUMENT_TYPES:
        problems.append(f"{name}: unknown document_type {record['document_type']!r}")
    if not DOC_ID.fullmatch(record["doc_id"] or ""):
        problems.append(f"{name}: doc_id {record['doc_id']!r} is not FG-DOC-nnn")
    if record["provenance"] not in PROVENANCES:
        problems.append(f"{name}: unknown provenance {record['provenance']!r}")
    if record["document_type"] != "META" and (
        record["evidence_strength"] not in EVIDENCE_STRENGTHS
    ):
        problems.append(f"{name}: missing evidence_strength")
    if any(vendor not in VENDORS for vendor in record["vendors"]):
        problems.append(f"{name}: unknown vendor in {record['vendors']}")
    if record["device_family"] not in (None, *DEVICE_FAMILIES):
        problems.append(f"{name}: unknown device_family {record['device_family']!r}")
    if record["transition_class"] not in (None, *TRANSITION_CLASSES):
        problems.append(
            f"{name}: unknown transition_class {record['transition_class']!r}"
        )

    if sha256(path) != record["sha256"]:
        problems.append(f"{name}: content changed since the manifest was reviewed")
    text = read_text(path)
    derived = describe(path, text)
    for key in derived:
        if key != "sha256" and derived[key] != record[key]:
            problems.append(
                f"{name}: manifest {key}={record[key]!r} but document says "
                f"{derived[key]!r}"
            )

    if record["document_type"] != "META":
        body_ids = DOC_ID.findall(text)
        if not body_ids or body_ids[0] != record["doc_id"]:
            problems.append(
                f"{name}: body identifies as {body_ids[:1]}, filename as "
                f"{record['doc_id']}"
            )
    body_vendor = _field(text, "Vendor", VENDORS)
    if len(record["vendors"]) == 1 and body_vendor != record["vendors"][0]:
        problems.append(f"{name}: body vendor {body_vendor!r} disagrees with name")
    if record["document_type"] == "CrossVendor":
        pair = [
            _field(text, "Primary Vendor", VENDORS),
            _field(text, "Dependent Vendor", VENDORS),
        ]
        if pair != record["vendors"]:
            problems.append(f"{name}: body vendor pair {pair} disagrees with name")
    body_family = _field(text, "Device Family", DEVICE_FAMILIES)
    if body_family is not None and body_family != record["device_family"]:
        problems.append(f"{name}: body device family {body_family!r} disagrees")
    if record["document_type"] == "Transition":
        category = _field(text, "Transition Category", ("Major", "Extreme"))
        if category is None or category.lower() != record["transition_class"]:
            problems.append(
                f"{name}: body transition category {category!r} disagrees with name"
            )
    upcoming = sorted(set(UPCOMING_REF.findall(text)))
    if upcoming:
        problems.append(f"{name}: references upcoming deployments {upcoming}")
    return problems


def _meta_strength_claims(text: str) -> dict[str, int]:
    """META's own table of how many documents carry each strength."""
    rows = re.findall(r"\|\s*(HIGH|MEDIUM|LOW)\s*\|[^|\n]*\|\s*(\d+)\s*\|", text)
    return {strength: int(count) for strength, count in rows}


def lint(
    corpus_dir: Path,
    deployment_ids: set[str] | None = None,
    *,
    meta_totals: bool = True,
) -> list[str]:
    """Every way the corpus disagrees with its manifest, or with itself.

    `deployment_ids` is the historical id set. It lives in the ignored data
    directory, so resolution is checked only where that file exists.
    `meta_totals` compares META's per-strength document counts with the
    documents; it is the check that notices a PDF extracting to nothing, and it
    only holds for the whole corpus.
    """
    manifest = load_manifest(corpus_dir)
    records = {record["filename"]: record for record in manifest["documents"]}
    files = {path.name: path for path in document_files(corpus_dir)}
    with (corpus_dir / INDEX_NAME).open(encoding="utf-8", newline="") as handle:
        indexed = {row["file"] for row in csv.DictReader(handle)}

    problems = []
    for name in sorted(set(files) ^ set(records)):
        where = "manifest" if name in records else "corpus directory"
        problems.append(f"{name}: present only in the {where}")
    if indexed != set(files):
        problems.append(
            f"{INDEX_NAME} disagrees with the directory: "
            f"{sorted(indexed ^ set(files))}"
        )
    doc_ids = Counter(record["doc_id"] for record in records.values())
    problems += [f"{doc_id}: used by {n} documents" for doc_id, n in doc_ids.items() if n > 1]

    for name in sorted(set(files) & set(records)):
        problems += _record_problems(records[name], files[name])

    meta = next(
        (files[name] for name, r in records.items() if r["document_type"] == "META"),
        None,
    )
    if meta is None:
        problems.append("no META document")
    elif meta_totals:
        claimed = _meta_strength_claims(read_text(meta))
        counted = Counter(
            r["evidence_strength"] for r in records.values() if r["evidence_strength"]
        )
        # A strength nobody carries is absent from the Counter but may be
        # claimed as 0, so compare on every strength.
        claimed = {s: claimed.get(s, 0) for s in EVIDENCE_STRENGTHS}
        actual = {s: counted.get(s, 0) for s in EVIDENCE_STRENGTHS}
        if claimed != actual:
            problems.append(
                f"META claims evidence strengths {claimed}, documents carry {actual}"
            )

    if deployment_ids is not None:
        for record in records.values():
            missing = sorted(set(record["source_deployment_ids"]) - deployment_ids)
            if missing:
                problems.append(
                    f"{record['filename']}: unresolved deployment ids {missing}"
                )
    return problems


def gold_consistency(
    manifest: dict[str, Any], events: Any
) -> list[dict[str, Any]]:
    """How far each document's cited deployments look like the document.

    P3 treats a document as relevant, by construction, to queries built from
    the deployments it cites. That holds only if those rows share the
    document's vendor (and device family, where it names one). This measures
    it rather than assuming it.
    """
    rows = events.set_index("deployment_id")
    report = []
    for record in manifest["documents"]:
        ids = [i for i in record["source_deployment_ids"] if i in rows.index]
        cited = rows.loc[ids]
        vendor_match = (
            cited["vendor_name"].isin(record["vendors"]).mean()
            if record["vendors"] and ids
            else None
        )
        family_match = (
            (cited["device_type"] == record["device_family"]).mean()
            if record["device_family"] and ids
            else None
        )
        report.append(
            {
                "doc_id": record["doc_id"],
                "document_type": record["document_type"],
                "cited": len(record["source_deployment_ids"]),
                "resolved": len(ids),
                "vendor_match": vendor_match,
                "family_match": family_match,
            }
        )
    return report
