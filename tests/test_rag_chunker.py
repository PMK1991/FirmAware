from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path

from firmaware.rag.chunker import (
    check_invariants,
    chunk_advisory,
    chunk_by_heading,
    chunk_corpus,
    chunk_policy,
    chunk_risknote,
    chunk_runbook,
    structure_warnings,
    tokens,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "corpus"
# Only the manifest is committed while publishing the documents is undecided,
# so "checked out" means the documents themselves, not the directory.
HAS_DOCUMENTS = CORPUS.is_dir() and any(
    p.suffix in {".md", ".pdf"} for p in CORPUS.iterdir()
)

try:  # PDFs parse through the optional "rag" extra
    import pdfplumber
except ImportError:  # pragma: no cover - exercised only without the extra
    pdfplumber = None

# Reviewed on 2026-09-27. A change beyond 10% on any type means the parser or a
# strategy moved; decide whether that was intended, then update this table.
BASELINE = {
    "Postmortem": 91,
    "Advisory": 54,
    "RiskNote": 24,
    "Runbook": 72,
    "Policy": 27,
    "Matrix": 4,
    "Transition": 56,
    "CrossVendor": 48,
    "META": 1,
}
# The PDFs whose structure extraction destroys. The three PDF runbooks are not
# here: their step numbering survives, and the runbook strategy splits on it.
COLLAPSED = {
    "FG-DOC-004", "FG-DOC-010",  # postmortems
    "FG-DOC-014", "FG-DOC-015", "FG-DOC-016",  # advisories
    "FG-DOC-020", "FG-DOC-022", "FG-DOC-025",
    "FG-DOC-048", "FG-DOC-049", "FG-DOC-050",  # policies
}

POSTMORTEM = """# Deployment Failure Postmortem
## Acme PLC — ROLLBACK Pattern
### Knowledge Base · FG-DOC-900

| Field | Value |
|---|---|
| Document ID | FG-DOC-900 |

## Root Cause Analysis
The bootloader was rewritten.

## Lessons Learned
Stage bootloader changes.

---

*Knowledge Base · Document ID: FG-DOC-900*
*Do not reference this document for upcoming deployments*
"""

POLICY = """# Test Policy
## Governance Policy · FG-POL-900

## Purpose
Keep plants running.

## Mandatory Rules
1. Kernel updates need review.
2. Bootloader updates need a site visit.

## Exception Process
Submit a deviation request to the architect.

## Compliance and Enforcement
Audits run quarterly.
"""


def _record(document_type: str, **overrides: object) -> dict:
    record = {
        "doc_id": "FG-DOC-900",
        "filename": f"{document_type}_Test_FG-DOC-900.md",
        "source_format": "md",
        "document_type": document_type,
        "retrieval_mode": "vector",
        "vendor": "Siemens",
        "vendors": ["Siemens"],
        "evidence_strength": "HIGH",
        "provenance": "internal_derived",
    }
    record.update(overrides)
    return record


class StrategyTests(unittest.TestCase):
    def test_heading_split_folds_the_title_and_drops_the_footer(self) -> None:
        chunks = chunk_by_heading(POSTMORTEM)
        self.assertEqual([kind for _, kind in chunks], ["prose"] * 3)
        header, root_cause, lessons = (text for text, _ in chunks)
        self.assertTrue(header.startswith("# Deployment Failure Postmortem"))
        self.assertIn("| Document ID | FG-DOC-900 |", header)
        self.assertTrue(root_cause.startswith("## Root Cause Analysis"))
        self.assertNotIn("Do not reference", lessons)

    def test_flat_text_is_kept_whole_rather_than_guessed_at(self) -> None:
        """What a PDF extracts to: the headings survive as words, not markup."""
        flat = POSTMORTEM.replace("## ", "").replace("# ", "")
        self.assertEqual([kind for _, kind in chunk_by_heading(flat)], ["whole"])

    def test_advisory_table_stays_whole_beside_prose_with_no_blank_line(self) -> None:
        text = (
            "# Advisory\n## Summary · FG-ADV-900\n\nIntro paragraph.\n\n"
            "## Flags\nFlags observed:\n| Flag | Seen |\n|---|---|\n"
            "| kernel_touched | Yes |\n| high_cvss | Yes |\n"
        )
        chunks = chunk_advisory(text)
        tables = [text for text, kind in chunks if kind == "table"]
        self.assertEqual(len(tables), 1)
        self.assertIn("## Flags", tables[0])
        self.assertIn("| Flag | Seen |\n|---|---|\n| kernel_touched", tables[0])
        self.assertIn("| high_cvss | Yes |", tables[0])

    def test_risknote_groups_are_balanced_inside_the_token_bounds(self) -> None:
        paragraph = " ".join(["word"] * 120)  # 156 tokens
        chunks = chunk_risknote("\n\n".join([paragraph] * 7))  # ~1,092 tokens
        self.assertEqual(len(chunks), 2)
        for text, _ in chunks:
            self.assertTrue(300 <= tokens(text) <= 700, tokens(text))

    def test_runbook_never_leaves_a_lone_step(self) -> None:
        steps = "\n".join(f"{n}. Do thing {n}." for n in range(1, 6))
        chunks = chunk_runbook(f"# Runbook\n\n## Execution\n{steps}\n")
        groups = [text for text, kind in chunks if kind == "step"]
        self.assertEqual([g.count("Do thing") for g in groups], [3, 2])
        self.assertTrue(all(g.startswith("## Execution") for g in groups))

    def test_pdf_runbook_rejoins_wrapped_steps_but_not_what_follows(self) -> None:
        text = (
            "Runbook\nVerification\n1. Check the ids DEP_000001 and\nDEP_000002.\n"
            "2. Confirm the image.\nExecution\n1. Isolate the device.\n"
            "2. Load the image.\nQuick Reference\nCondition Action\n"
        )
        steps = [t for t, kind in chunk_runbook(text) if kind == "step"]
        self.assertEqual(steps[0].split("\n")[0], "Verification")
        self.assertIn("1. Check the ids DEP_000001 and DEP_000002.", steps[0])
        self.assertEqual(steps[1].split("\n")[0], "Execution")
        self.assertNotIn("Quick Reference", steps[1])

    def test_every_rule_carries_the_exception_process(self) -> None:
        chunks = chunk_policy(POLICY)
        rules = [text for text, kind in chunks if kind == "rule"]
        self.assertEqual(len(rules), 2)
        for rule in rules:
            self.assertIn("Submit a deviation request", rule)
            self.assertTrue(rule.startswith("# Test Policy\n## Mandatory Rules"))
        # Carried by every rule, so not repeated as a chunk of its own.
        prose = [text for text, kind in chunks if kind == "prose"]
        self.assertFalse(any(t.startswith("## Exception Process") for t in prose))

    def test_policy_without_an_exception_section_is_not_split(self) -> None:
        """A rule chunk that silently lost its exception would read as absolute."""
        text = POLICY.replace("## Exception Process", "## Notes")
        self.assertEqual([kind for _, kind in chunk_policy(text)], ["whole"])


class InvariantTests(unittest.TestCase):
    """Each invariant fires on the defect it names."""

    def _violations(self, document_type: str, pieces, **record) -> list[str]:
        base = _record(document_type, **record)
        chunks = [
            {**base, "chunk_index": i, "chunk_id": f"FG-DOC-900_chunk_{i}",
             "chunk_kind": kind, "text": text}
            for i, (text, kind) in enumerate(pieces)
        ]
        return check_invariants(chunks)

    def test_merged_sections(self) -> None:
        text = "## Root Cause\nA.\n## Lessons Learned\nB."
        self.assertIn("merges sections", str(self._violations("Postmortem", [(text, "prose")])))

    def test_chunk_not_starting_at_a_heading(self) -> None:
        text = "Orphaned paragraph.\n## Root Cause\nA."
        self.assertIn("heading", str(self._violations("Postmortem", [(text, "prose")])))

    def test_table_cut_from_its_header(self) -> None:
        text = "## Flags\n\n| kernel_touched | Yes |\n| high_cvss | Yes |"
        self.assertIn("header", str(self._violations("Advisory", [(text, "table")])))

    def test_risknote_outside_token_bounds(self) -> None:
        text = " ".join(["word"] * 600)
        self.assertIn("tokens", str(self._violations("RiskNote", [(text, "prose")])))

    def test_lone_and_non_contiguous_steps(self) -> None:
        lone = self._violations("Runbook", [("## Execution\n4. Only step.", "step")])
        self.assertIn("fewer than two steps", str(lone))
        gap = self._violations("Runbook", [("## Execution\n1. A.\n3. C.", "step")])
        self.assertIn("not contiguous", str(gap))

    def test_rule_without_exception_and_two_rules_in_one_chunk(self) -> None:
        problems = str(
            self._violations("Policy", [("## Mandatory Rules\n1. A.\n2. B.", "rule")])
        )
        self.assertIn("2 rules in one chunk", problems)
        self.assertIn("without its exception clause", problems)

    def test_cross_vendor_chunk_naming_a_third_vendor(self) -> None:
        text = "## Cascade\nSiemens faults reached Cisco and Honeywell."
        problems = self._violations(
            "CrossVendor", [(text, "prose")], vendors=["Siemens", "Cisco"]
        )
        self.assertIn("outside its pair", str(problems))

    def test_matrix_split_in_two(self) -> None:
        problems = self._violations("Matrix", [("A", "whole"), ("B", "whole")])
        self.assertIn("exactly one chunk", str(problems))


@unittest.skipIf(pdfplumber is None, "the rag extra is not installed")
@unittest.skipIf(not HAS_DOCUMENTS, "the corpus documents are not checked out")
class CorpusChunkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.chunks = chunk_corpus(CORPUS)

    def test_every_invariant_holds_across_the_corpus(self) -> None:
        self.assertEqual(check_invariants(self.chunks), [])

    def test_chunk_counts_per_type_stay_within_ten_percent_of_baseline(self) -> None:
        counts = Counter(c["document_type"] for c in self.chunks)
        for document_type, expected in BASELINE.items():
            self.assertLessEqual(
                abs(counts[document_type] - expected),
                0.1 * expected,
                f"{document_type}: {counts[document_type]} against baseline {expected}",
            )

    def test_pdf_collapse_is_reproduced_and_flagged_not_accepted(self) -> None:
        """HLD acceptance 2: the known PDF failure is visible, and nothing else is."""
        warned = {w.split(" ")[0] for w in structure_warnings(self.chunks)}
        self.assertEqual(warned, COLLAPSED)
        for chunk in self.chunks:
            if chunk["doc_id"] in COLLAPSED:
                self.assertEqual(chunk["chunk_kind"], "whole")

    def test_each_policy_rule_retrieved_carries_its_exception(self) -> None:
        """HLD acceptance 5, on every markdown policy."""
        rules = [c for c in self.chunks if c["chunk_kind"] == "rule"]
        self.assertEqual({c["doc_id"] for c in rules}, {"FG-DOC-046", "FG-DOC-047"})
        for rule in rules:
            self.assertIn("## Exception Process\nEngineers", rule["text"])

    def test_chunks_carry_the_manifest_metadata(self) -> None:
        by_doc = {c["doc_id"]: c for c in self.chunks}
        self.assertEqual(by_doc["FG-DOC-068"]["vendors"], ["Cisco", "Honeywell"])
        self.assertEqual(by_doc["FG-DOC-047"]["vendor"], "vendor-neutral")
        self.assertEqual({c["provenance"] for c in self.chunks}, {"internal_derived"})
        self.assertEqual(len({c["doc_id"] for c in self.chunks}), 69)
