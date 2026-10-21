from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from firmaware.rag.corpus import (
    META_DOC_ID,
    build_manifest,
    document_files,
    lint,
    load_manifest,
    write_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "corpus"
EVENTS = ROOT / "data" / "deployment_events.csv"
# Written for the tests, copying no real document, so the lint rules stay
# tested whether or not the corpus itself is checked in.
FIXTURE = ROOT / "tests" / "fixtures" / "rag_corpus"

try:  # PDFs parse through the optional "rag" extra
    import pdfplumber
except ImportError:  # pragma: no cover - exercised only without the extra
    pdfplumber = None

# Markdown only, so the mutation tests need no PDF parser.
SLICE = [
    "META_Fixture_Knowledge_Base.md",
    "Postmortem_Siemens_PLC_ROLLBACK_FG-DOC-901.md",
    "CrossVendor_Siemens_to_Cisco_FG-DOC-902.md",
    "Policy_Fixture_Kernel_Policy_FG-DOC-903.md",
]


def _slice_corpus(directory: Path) -> Path:
    for name in [*SLICE, "corpus_index.csv"]:
        shutil.copy2(FIXTURE / name, directory / name)
    write_manifest(directory, build_manifest(directory, "test fixture"))
    return directory


@unittest.skipIf(not CORPUS.exists(), "the corpus is not checked out")
class CorpusContractTests(unittest.TestCase):
    """The committed corpus as it stands."""

    @unittest.skipIf(pdfplumber is None, "the rag extra is not installed")
    def test_committed_corpus_agrees_with_its_manifest(self) -> None:
        self.assertEqual(lint(CORPUS), [])

    def test_corpus_is_the_sixty_nine_documents_the_plan_names(self) -> None:
        records = load_manifest(CORPUS)["documents"]
        self.assertEqual(len(records), 69)
        self.assertEqual(len(document_files(CORPUS)), 69)
        self.assertEqual(sum(r["source_format"] == "pdf" for r in records), 14)

    def test_every_document_is_internally_derived(self) -> None:
        """The corpus was generated from the training records, so it has no
        independent slice. Marking a document external would fabricate one and
        switch on provenance weighting that nothing earned."""
        records = load_manifest(CORPUS)["documents"]
        self.assertEqual({r["provenance"] for r in records}, {"internal_derived"})

    def test_vendor_scope_is_explicit_for_every_document(self) -> None:
        records = {r["doc_id"]: r for r in load_manifest(CORPUS)["documents"]}
        self.assertEqual(records[META_DOC_ID]["document_type"], "META")
        self.assertEqual(records["FG-DOC-068"]["vendor"], "cross-vendor")
        self.assertEqual(records["FG-DOC-068"]["vendors"], ["Cisco", "Honeywell"])
        for record in records.values():
            if record["document_type"] in {"Policy", "Matrix", "META"}:
                self.assertEqual(record["vendor"], "vendor-neutral")
                self.assertEqual(record["vendors"], [])

    def test_malformed_deployment_refs_are_known_and_out_of_the_gold_set(self) -> None:
        """Short ids cannot be padded safely, so they are recorded, not repaired.
        Any new one fails the lint, because the manifest pins this list."""
        malformed = {
            r["doc_id"]: r["malformed_refs"]
            for r in load_manifest(CORPUS)["documents"]
            if r["malformed_refs"]
        }
        self.assertEqual(
            malformed,
            {
                "FG-DOC-002": ["DEP_00003", "DEP_0143"],
                "FG-DOC-005": ["DEP_00004"],
                "FG-DOC-007": ["DEP_00118"],
                # The PDF's text ends mid-sentence on its last page; two parsers agree.
                "FG-DOC-010": ["DEP_00"],
                "FG-DOC-064": ["DEP_05190"],
            },
        )

    @unittest.skipIf(not EVENTS.exists(), "deployment_events.csv is not checked in")
    def test_every_cited_deployment_resolves_to_a_historical_record(self) -> None:
        ids = set(pd.read_csv(EVENTS, usecols=["deployment_id"])["deployment_id"])
        problems = [p for p in lint(CORPUS, ids) if "unresolved" in p]
        self.assertEqual(problems, [])


class CorpusLintTests(unittest.TestCase):
    """Each lint rule fires on the defect it exists for."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.corpus = _slice_corpus(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint(self, deployment_ids: set[str] | None = None) -> list[str]:
        # META's per-strength totals describe the whole corpus, not a slice.
        return lint(self.corpus, deployment_ids, meta_totals=False)

    def _append(self, name: str, text: str) -> None:
        with (self.corpus / name).open("a", encoding="utf-8") as handle:
            handle.write(text)

    def test_clean_slice_passes(self) -> None:
        self.assertEqual(self._lint(), [])

    def test_meta_totals_count_strengths_nobody_carries_as_zero(self) -> None:
        self.assertEqual(lint(self.corpus), [])

    def test_transition_category_must_agree_with_the_name(self) -> None:
        name = "Transition_Siemens_major_FG-DOC-905.md"
        (self.corpus / name).write_text(
            "# Siemens Transition\n| Document ID | FG-DOC-905 |\n| Vendor | Siemens |\n"
            "| Transition Category | Extreme version jump |\n| Evidence Strength | HIGH |\n",
            encoding="utf-8",
        )
        with (self.corpus / "corpus_index.csv").open("a", encoding="utf-8") as handle:
            handle.write(f"{name},md,Transition\n")
        write_manifest(self.corpus, build_manifest(self.corpus, "test fixture"))
        self.assertTrue(any("transition category" in p for p in self._lint()))

    def test_reference_to_an_upcoming_deployment_fails(self) -> None:
        self._append(SLICE[1], "\nCompare UPC_00012 before approving.\n")
        self.assertTrue(any("upcoming deployments" in p for p in self._lint()))

    def test_meta_explaining_the_upcoming_prefix_is_not_a_reference(self) -> None:
        self._append(SLICE[0], "\nUpcoming records use the (UPC_ prefix).\n")
        self.assertFalse(any("upcoming" in p for p in self._lint()))

    def test_new_malformed_deployment_ref_fails(self) -> None:
        self._append(SLICE[1], "\nSee also DEP_0042.\n")
        self.assertTrue(any("malformed_refs" in p for p in self._lint()))

    def test_edited_document_fails_until_the_manifest_is_reviewed(self) -> None:
        self._append(SLICE[2], "\nAn unreviewed paragraph.\n")
        self.assertTrue(any("content changed" in p for p in self._lint()))

    def test_body_identifying_as_another_document_fails(self) -> None:
        path = self.corpus / SLICE[1]
        path.write_text(
            path.read_text(encoding="utf-8").replace("FG-DOC-901", "FG-DOC-999"),
            encoding="utf-8",
        )
        self.assertTrue(any("body identifies as" in p for p in self._lint()))

    def test_missing_manifest_field_fails(self) -> None:
        manifest = load_manifest(self.corpus)
        del manifest["documents"][1]["source_deployment_ids"]
        write_manifest(self.corpus, manifest)
        self.assertTrue(any("missing" in p for p in self._lint()))

    def test_unknown_document_type_fails(self) -> None:
        manifest = load_manifest(self.corpus)
        manifest["documents"][1]["document_type"] = "Whitepaper"
        write_manifest(self.corpus, manifest)
        self.assertTrue(any("unknown document_type" in p for p in self._lint()))

    def test_document_absent_from_the_index_fails(self) -> None:
        (self.corpus / "Postmortem_Siemens_RTU_ROLLBACK_FG-DOC-904.md").write_text(
            "# Postmortem\n| Document ID | FG-DOC-904 |\n| Vendor | Siemens |\n",
            encoding="utf-8",
        )
        problems = self._lint()
        self.assertTrue(any("present only in the corpus directory" in p for p in problems))
        self.assertTrue(any("corpus_index.csv disagrees" in p for p in problems))

    def test_unresolved_deployment_id_fails(self) -> None:
        problems = self._lint(deployment_ids={"DEP_000001"})
        self.assertTrue(any("unresolved deployment ids" in p for p in problems))


class RagIsolationTests(unittest.TestCase):
    def test_cloud_sdks_load_only_where_a_cloud_is_called(self) -> None:
        """Chunking and eval must run on a laptop with no cloud credentials.

        The rule is io.py's: a cloud SDK may be imported inside the function
        that talks to the cloud, never at module level. Two modules call Azure:
        the Foundry generator and the AI Search backend, each importing
        azure-identity once, when it is first configured. Nothing else in the
        package touches Azure, so the offline path never does."""
        rag = ROOT / "src" / "firmaware" / "rag"
        for path in rag.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for sdk in ("azure", "google", "openai"):
                self.assertNotRegex(source, rf"(?m)^(from|import)\s+{sdk}\b", path.name)
        azure_facing = {"generate.py": 1, "azure_search.py": 1}
        for name, expected in azure_facing.items():
            source = (rag / name).read_text(encoding="utf-8")
            imports = re.findall(r"(?m)^\s+(?:from|import)\s+azure\S*", source)
            self.assertEqual(len(imports), expected, f"{name}: azure-identity, once")
        for path in rag.glob("*.py"):
            if path.name not in azure_facing:
                self.assertNotRegex(path.read_text(encoding="utf-8"),
                                    r"(?m)^\s*(from|import)\s+azure\b", path.name)

    def test_heavy_dependencies_load_only_inside_functions(self) -> None:
        """torch (via sentence-transformers) and the Anthropic SDK are in the
        notebook extra; a module-level import would break every install without
        it, CI's included."""
        for path in (ROOT / "src" / "firmaware" / "rag").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for heavy in ("anthropic", "sentence_transformers", "dotenv"):
                self.assertNotRegex(source, rf"(?m)^(from|import)\s+{heavy}\b", path.name)
