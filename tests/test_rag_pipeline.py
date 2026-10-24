from __future__ import annotations

import hashlib
import io
import json
import os
import re
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from firmaware.rag.evaluate import (
    evaluate,
    evaluate_generation,
    generation_probes,
    gold_queries,
    rejudge,
)
from firmaware.rag.explain import NO_FLAG_INSTRUCTION, explain
from firmaware.rag.flags import deployment_flags, transition_class
from firmaware.rag.gate import GateConfig, assess
from firmaware.rag.generate import (
    FoundryGenerator,
    Generation,
    generator_from_env,
    grounding_checks,
    judge_faithfulness,
    mark_unretrieved,
)
from firmaware.rag.retrieval import Hit, LocalIndex, build_query, retrieve
from tests.test_schema import make_training_frame


class CountingEmbedder:
    """Deterministic bag-of-words vectors, counting every text it embeds."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        vectors = np.zeros((len(texts), 256), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9_]+", text.lower()):
                vectors[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)


class CountingGenerator:
    name = "counting"

    def __init__(self, text: str = "Kernel changes drove rollbacks [FG-DOC-001].") -> None:
        self.calls = 0
        self.text = text
        self.last_user = ""

    def generate(self, system: str, user: str) -> Generation:
        self.calls += 1
        self.last_user = user
        return Generation(self.text, "end_turn")


def _record(doc_id, document_type, vendors=(), device_family=None, band=None, cites=(),
            scope=None):
    return {"doc_id": doc_id, "document_type": document_type, "vendors": list(vendors),
            "device_family": device_family, "transition_class": band,
            "site_criticality_scope": scope, "source_deployment_ids": list(cites)}


def _chunk(doc_id, document_type, index, text, vendors=(), kind="prose"):
    return {"chunk_id": f"{doc_id}_chunk_{index}", "doc_id": doc_id, "chunk_index": index,
            "chunk_kind": kind, "document_type": document_type, "vendors": list(vendors),
            "text": text}


RECORDS = [
    _record("FG-DOC-001", "Postmortem", ["Siemens"], "PLC", cites=["DEP_000001", "DEP_000009"]),
    _record("FG-DOC-002", "Advisory", ["Siemens"], "PLC"),
    _record("FG-DOC-003", "RiskNote", ["Siemens"], "PLC"),
    _record("FG-DOC-004", "Runbook", ["Siemens"], "PLC", cites=["DEP_000002"]),
    _record("FG-DOC-005", "Transition", ["Siemens"], band="major", cites=["DEP_000003"]),
    _record("FG-DOC-006", "CrossVendor", ["Siemens", "Cisco"]),
    _record("FG-DOC-007", "CrossVendor", ["Cisco", "Siemens"]),
    _record("FG-DOC-008", "Policy", scope="HIGH"),
    _record("FG-DOC-009", "Matrix"),
    _record("FG-DOC-010", "Policy", scope="MEDIUM"),
]
CHUNKS = [
    _chunk("FG-DOC-001", "Postmortem", 0, "Siemens PLC kernel rollback after a major version jump", ["Siemens"]),
    _chunk("FG-DOC-002", "Advisory", 0, "Siemens PLC advisory: kernel_touched upgrades fail", ["Siemens"]),
    _chunk("FG-DOC-003", "RiskNote", 0, "Siemens PLC risk note on kernel failures at high criticality sites", ["Siemens"]),
    _chunk("FG-DOC-004", "Runbook", 0, "Siemens PLC rollback runbook header", ["Siemens"]),
    _chunk("FG-DOC-004", "Runbook", 1, "1. Verify the image.\n2. Isolate the PLC.", ["Siemens"], "step"),
    _chunk("FG-DOC-004", "Runbook", 2, "3. Load the image.\n4. Reboot.", ["Siemens"], "step"),
    _chunk("FG-DOC-005", "Transition", 0, "Siemens major version transition risk", ["Siemens"]),
    _chunk("FG-DOC-006", "CrossVendor", 0, "Siemens to Cisco cascade", ["Siemens", "Cisco"]),
    _chunk("FG-DOC-007", "CrossVendor", 0, "Cisco to Siemens cascade", ["Cisco", "Siemens"]),
    _chunk("FG-DOC-008", "Policy", 0, "# Policy\n## Mandatory Rules\n1. kernel_touched needs review.\n\n"
           "## Exception Process\nAsk.", kind="rule"),
    _chunk("FG-DOC-008", "Policy", 1, "kernel bootloader cvss emergency protocol_mismatch everything",
           kind="whole"),
    _chunk("FG-DOC-009", "Matrix", 0, "| Vendor | Risk |\n| Siemens | HIGH |\n| Honeywell | LOW |",
           kind="whole"),
    _chunk("FG-DOC-010", "Policy", 0, "# Policy\n## Mandatory Rules\n1. kernel_touched needs a lab.\n\n"
           "## Exception Process\nAsk.", kind="rule"),
]


def _row(**overrides) -> pd.Series:
    values = {
        "vendor_name": "Siemens", "device_type": "PLC", "current_firmware": "4.2.3",
        "target_firmware": "5.0.1", "max_cvss_score": 7.9, "site_criticality": "HIGH",
        "deployment_type": "SCHEDULED", "transition_class": "major",
        "cross_vendor_dependency_count": 2, "active_flags": ["kernel_touched"],
    }
    values.update(overrides)
    return pd.Series(values, name=overrides.get("name", "UPC_00001"))


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.embedder = CountingEmbedder()
        self.index = LocalIndex(CHUNKS, self.embedder)
        self.embedder.calls = 0

    def _retrieve(self, **overrides):
        row = _row(**overrides)
        return retrieve(row, list(row["active_flags"]), RECORDS, self.index)

    def test_each_route_reaches_only_its_own_documents(self) -> None:
        routes = self._retrieve()
        self.assertEqual({h.chunk["document_type"] for h in routes["evidence"]},
                         {"Postmortem", "Advisory", "RiskNote"})
        self.assertEqual({h.chunk["doc_id"] for h in routes["transition"]}, {"FG-DOC-005"})
        self.assertEqual({h.chunk["doc_id"] for h in routes["runbook"]}, {"FG-DOC-004"})
        self.assertEqual({h.chunk["doc_id"] for h in routes["matrix"]}, {"FG-DOC-009"})

    def test_the_query_is_embedded_once_across_every_vector_route(self) -> None:
        routes = self._retrieve()
        self.assertTrue(routes["evidence"] and routes["transition"] and routes["cross_vendor"])
        self.assertEqual(self.embedder.calls, 1)

    def test_vectorless_routes_make_no_embedding_call(self) -> None:
        """HLD acceptance 6: a matrix lookup answers with zero embedding calls.
        Honeywell is in the matrix but has no vector-route documents, so the
        matrix and policy hits below came from the metadata routes alone."""
        routes = self._retrieve(vendor_name="Honeywell", device_type="PLC")
        self.assertEqual(self.embedder.calls, 0)
        self.assertEqual(routes["evidence"], [])
        self.assertEqual({h.chunk["doc_id"] for h in routes["matrix"]}, {"FG-DOC-009"})
        self.assertTrue(routes["policy"])

    def test_no_fallback_to_a_neighbouring_device_or_band(self) -> None:
        routes = self._retrieve(device_type="HMI", transition_class=None)
        self.assertEqual(routes["runbook"], [])
        self.assertEqual(routes["transition"], [])

    def test_cross_vendor_takes_the_primary_vendor_and_needs_a_dependency(self) -> None:
        self.assertEqual({h.chunk["doc_id"] for h in self._retrieve()["cross_vendor"]},
                         {"FG-DOC-006"})
        self.assertEqual(self._retrieve(cross_vendor_dependency_count=0)["cross_vendor"], [])

    def test_policy_rules_outrank_a_collapsed_whole_document(self) -> None:
        policy = self._retrieve(active_flags=["kernel_touched", "bootloader_touched"])["policy"]
        self.assertEqual(policy[0].chunk["chunk_kind"], "rule")

    def test_a_policy_reaches_only_the_site_criticality_it_declares(self) -> None:
        # Seen live: a HIGH-only policy's rule, matched on major_version_jump,
        # was recommended for a MEDIUM site. Keyword overlap is not scope.
        def policies(criticality):
            routes = self._retrieve(site_criticality=criticality)
            return {h.chunk["doc_id"] for h in routes["policy"]}

        self.assertEqual(policies("HIGH"), {"FG-DOC-008"})
        self.assertEqual(policies("MEDIUM"), {"FG-DOC-010"})
        self.assertEqual(policies("LOW"), set())

    def test_query_carries_no_identifier(self) -> None:
        query = build_query(_row(), ["kernel_touched"])
        self.assertNotRegex(query, r"(DEP|UPC|DEV|SITE)_\d")


class GateTests(unittest.TestCase):
    def _hits(self, n, vendor="Siemens", text="kernel failure", docs=None):
        return [Hit({"doc_id": (docs or [f"D{i}" for i in range(n)])[i], "vendors": [vendor],
                     "text": text}, 0.9, "evidence") for i in range(n)]

    def test_no_vendor_evidence_is_insufficient(self) -> None:
        self.assertEqual(assess(self._hits(5, "Cisco"), "Siemens", []).verdict,
                         "insufficient_evidence")

    def test_thin_or_single_document_evidence_is_low_confidence(self) -> None:
        self.assertEqual(assess(self._hits(2), "Siemens", []).verdict, "low_confidence")
        same = self._hits(3, docs=["D0", "D0", "D0"])
        self.assertEqual(assess(same, "Siemens", []).verdict, "low_confidence")

    def test_ungrounded_flags_are_low_confidence_for_that_reason(self) -> None:
        result = assess(self._hits(3), "Siemens", ["high_network_stress"])
        self.assertEqual(result.verdict, "low_confidence")
        self.assertEqual(result.reasons, ["0/1 active flags grounded in the evidence"])

    def test_flagless_deployment_is_no_flag_evidence_not_no_concern(self) -> None:
        self.assertEqual(assess(self._hits(3), "Siemens", []).verdict, "no_flag_evidence")
        self.assertEqual(assess(self._hits(3), "Siemens", ["kernel_touched"]).verdict,
                         "concern_found")

    def test_score_threshold_is_off_by_default_and_fires_when_calibrated(self) -> None:
        self.assertEqual(assess(self._hits(3), "Siemens", []).verdict, "no_flag_evidence")
        strict = GateConfig(min_mean_score=0.95)
        self.assertEqual(assess(self._hits(3), "Siemens", [], strict).verdict, "low_confidence")


class ExplainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = LocalIndex(CHUNKS, CountingEmbedder())

    def test_gated_out_deployments_never_reach_the_generator(self) -> None:
        generator = CountingGenerator()
        unknown = explain(_row(vendor_name="Moxa"), RECORDS, self.index, generator)
        ungrounded = explain(_row(active_flags=["high_network_stress"]), RECORDS, self.index,
                             generator)
        self.assertEqual(unknown.assessment.verdict, "insufficient_evidence")
        self.assertEqual(ungrounded.assessment.verdict, "low_confidence")
        self.assertEqual(generator.calls, 0)

    def test_a_confident_deployment_generates_once_and_is_checked(self) -> None:
        generator = CountingGenerator()
        result = explain(_row(), RECORDS, self.index, generator)
        self.assertEqual(result.assessment.verdict, "concern_found")
        self.assertEqual(generator.calls, 1)
        self.assertEqual(result.generated_by, "counting")
        self.assertTrue(result.checks["citations_ok"])

    def test_a_flagless_deployment_is_told_the_evidence_does_not_explain_it(self) -> None:
        generator = CountingGenerator()
        result = explain(_row(active_flags=[]), RECORDS, self.index, generator)
        self.assertEqual(result.assessment.verdict, "no_flag_evidence")
        self.assertIn(NO_FLAG_INSTRUCTION, generator.last_user)
        flagged = CountingGenerator()
        explain(_row(), RECORDS, self.index, flagged)
        self.assertNotIn(NO_FLAG_INSTRUCTION, flagged.last_user)

    def test_the_score_is_given_as_a_percentage_too(self) -> None:
        """So "about 98%" in an explanation is grounded, not a stray number."""
        row = _row(risk_probability=0.9816, risk_prediction="NO_GO", risk_band="HIGH")
        context = explain(row, RECORDS, self.index, None).context
        self.assertIn("probability 0.9816, about 98%", context)
        cited = [Hit({"doc_id": "FG-DOC-001", "text": ""}, 1.0, "evidence")]
        checks = grounding_checks("About 98% risk [FG-DOC-001].", cited, context, [])
        self.assertTrue(checks["numbers_ok"], checks)

    def test_the_major_version_and_the_magnitude_are_told_apart(self) -> None:
        # The writer explained major_version_jump by the magnitude when it saw
        # only the magnitude; the context now carries what sets the flag.
        context = explain(_row(version_jump_magnitude=1.0), RECORDS, self.index, None).context
        self.assertIn("Major version: 4 -> 5\n", context)
        self.assertIn("Version jump magnitude: 1.0 (not the major_version_jump flag)", context)

    def test_a_failed_call_is_an_error_not_an_explanation(self) -> None:
        class Refusing:
            name = "refusing"

            def generate(self, system: str, user: str) -> Generation:
                return Generation(None, "API error 400: not scoped to a workspace")

        result = explain(_row(), RECORDS, self.index, Refusing())
        self.assertEqual(result.assessment.verdict, "concern_found")
        self.assertIsNone(result.text)
        self.assertIsNone(result.generated_by)
        self.assertIn("workspace", result.error)
        self.assertEqual(result.checks, {})

    def test_without_a_generator_it_still_retrieves_and_gates(self) -> None:
        result = explain(_row(), RECORDS, self.index, None)
        self.assertEqual(result.assessment.verdict, "concern_found")
        self.assertIsNone(result.text)


GROUNDING_HITS = [
    Hit({"doc_id": "FG-DOC-001", "text": "400 rollbacks with kernel_touched at CVSS 4.61."},
        0.9, "evidence")
]


class FoundryGeneratorTests(unittest.TestCase):
    """No network: the HTTP call and the Entra token are replaced."""

    def _generator(self):
        generator = object.__new__(FoundryGenerator)  # skip azure-identity, absent in CI
        generator.name = "Llama-3.3-70B-Instruct"
        generator._url = "https://example.invalid/openai/v1/chat/completions"
        generator._token = lambda: "token"
        generator._max_tokens = 100
        generator._timeout = 5
        generator._temperature = 0.2
        return generator

    def _respond(self, payload: dict):
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response
        return patch("urllib.request.urlopen", return_value=response)

    def test_a_finished_answer_is_returned(self) -> None:
        with self._respond({"choices": [{"finish_reason": "stop",
                                         "message": {"content": "Grounded."}}]}) as call:
            generation = self._generator().generate("system", "user")
        self.assertEqual(generation, Generation("Grounded.", "end_turn"))
        sent = json.loads(call.call_args.args[0].data)
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user"])
        self.assertEqual(call.call_args.args[0].headers["Authorization"], "Bearer token")
        self.assertEqual(sent["temperature"], 0.2)

    def test_a_truncated_answer_is_not_an_explanation(self) -> None:
        with self._respond({"choices": [{"finish_reason": "length",
                                         "message": {"content": "Half an"}}]}):
            generation = self._generator().generate("system", "user")
        self.assertIsNone(generation.text)
        self.assertEqual(generation.stop_reason, "length")

    def test_an_http_error_is_reported_not_raised(self) -> None:
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"no role"))
        with patch("urllib.request.urlopen", side_effect=error):
            generation = self._generator().generate("system", "user")
        self.assertIsNone(generation.text)
        self.assertIn("401", generation.stop_reason)

    def test_foundry_wins_when_configured_and_nothing_means_no_generator(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(generator_from_env())
        configured = {"FIRMAWARE_FOUNDRY_ENDPOINT": "https://example.invalid",
                      "FIRMAWARE_FOUNDRY_DEPLOYMENT": "Llama-3.3-70B-Instruct",
                      "ANTHROPIC_API_KEY": "also-set"}
        with patch.dict(os.environ, configured, clear=True), \
                patch.object(FoundryGenerator, "__init__", return_value=None) as built:
            self.assertIsInstance(generator_from_env(), FoundryGenerator)
        built.assert_called_once_with("https://example.invalid", "Llama-3.3-70B-Instruct")


class ScriptedJudge:
    name = "judge"

    def __init__(self, reply: str | None) -> None:
        self.reply = reply

    def generate(self, system: str, user: str) -> Generation:
        return Generation(self.reply, "end_turn" if self.reply else "length")


JUDGED_HITS = (
    Hit({"doc_id": "FG-DOC-001", "document_type": "Postmortem", "text": "x"}, 1.0, "evidence"),
)


class GenerationEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = LocalIndex(CHUNKS, CountingEmbedder())
        self.upcoming = pd.DataFrame([
            _row(name="UPC_00001", risk_probability=0.9),
            _row(name="UPC_00002", risk_probability=0.2, active_flags=[]),
            _row(name="UPC_00003", vendor_name="Moxa", risk_probability=0.5),
        ])

    def test_judge_reply_is_parsed_and_unparseable_is_none_not_a_pass(self) -> None:
        ok = judge_faithfulness(ScriptedJudge(
            'Sure: {"claims": [{"claim": "a", "verdict": "supported"}]}'), "ctx", JUDGED_HITS, "t")
        self.assertTrue(ok["faithful"])
        self.assertEqual(ok["precision"], 1.0)
        bad = judge_faithfulness(ScriptedJudge("I think it is fine."), "ctx", JUDGED_HITS, "t")
        self.assertIsNone(bad["faithful"])
        self.assertIsNone(bad["precision"])
        cut = judge_faithfulness(ScriptedJudge(None), "ctx", JUDGED_HITS, "t")
        self.assertIsNone(cut["faithful"])

    def test_precision_is_per_claim_and_an_unreadable_verdict_is_not_a_pass(self) -> None:
        reply = ('{"claims": [{"claim": "a", "verdict": "supported"},'
                 ' {"claim": "b", "verdict": "supported"},'
                 ' {"claim": "c", "verdict": "unsupported"},'
                 ' {"claim": "d", "verdict": "maybe"},'
                 ' {"claim": "e", "verdict": "not_a_claim"}]}')
        verdict = judge_faithfulness(ScriptedJudge(reply), "ctx", JUDGED_HITS, "t")
        self.assertEqual(verdict["precision"], 0.5)  # 2 supported of 4 judged
        self.assertEqual(verdict["unsupported_claims"], ["c", "d"])
        self.assertFalse(verdict["faithful"])

    def test_a_statement_of_absence_is_never_counted_against_the_writer(self) -> None:
        """The judge listed this as unsupported in three runs despite being told not to."""
        reply = ('{"claims": [{"claim": "the historical evidence provided does not explain'
                 ' this score", "verdict": "unsupported"},'
                 ' {"claim": "Rollbacks followed kernel changes", "verdict": "supported"}]}')
        verdict = judge_faithfulness(ScriptedJudge(reply), "ctx", JUDGED_HITS, "t")
        self.assertEqual(verdict["precision"], 1.0)
        self.assertTrue(verdict["faithful"])

    def test_probes_cover_vendors_and_refusals_deterministically(self) -> None:
        probes = [name for name, _ in generation_probes(RECORDS, self.upcoming)]
        again = [name for name, _ in generation_probes(RECORDS, self.upcoming)]
        self.assertEqual(probes, again)
        self.assertIn("out-of-corpus Moxa UPC_00003", probes)
        self.assertIn("synthetic out-of-corpus vendor", probes)
        self.assertTrue(any(name.startswith("Siemens ") for name in probes))

    def test_a_grounded_writer_and_faithful_judge_pass_every_gate(self) -> None:
        writer = CountingGenerator("Kernel changes drove rollbacks [FG-DOC-001].")
        judge = ScriptedJudge('{"claims": [{"claim": "Kernel changes drove rollbacks",'
                              ' "verdict": "supported"}]}')
        report = evaluate_generation(RECORDS, self.index, self.upcoming, writer, judge)
        self.assertTrue(report["passed"], report["gates"])
        self.assertTrue(all("context" in r for r in report["results"]))
        refused = [r for r in report["results"] if r["vendor"] != "Siemens"]
        self.assertTrue(refused and all(not r["generated"] for r in refused))

    def test_an_invented_number_or_unfaithful_verdict_fails_its_gate(self) -> None:
        writer = CountingGenerator("Exactly 73 rollbacks happened [FG-DOC-001].")
        judge = ScriptedJudge('{"claims": [{"claim": "Exactly 73 rollbacks happened",'
                              ' "verdict": "unsupported"}]}')
        report = evaluate_generation(RECORDS, self.index, self.upcoming, writer, judge)
        self.assertFalse(report["gates"]["numbers_ok"])
        self.assertFalse(report["gates"]["judge_claim_precision"])
        self.assertFalse(report["passed"])

    def test_rejudge_regrades_saved_text_without_the_writer(self) -> None:
        writer = CountingGenerator("Kernel changes drove rollbacks [FG-DOC-001].")
        judge = ScriptedJudge('{"claims": [{"claim": "k", "verdict": "supported"}]}')
        report = evaluate_generation(RECORDS, self.index, self.upcoming, writer, judge)
        calls = writer.calls
        again = rejudge(report, RECORDS, self.index, self.upcoming, judge, repeat=2)
        self.assertEqual(writer.calls, calls)
        self.assertEqual(len(again["passes"]), 2)
        self.assertEqual(again["precision_mean"], 1.0)
        self.assertEqual(again["precision_spread"], 0.0)


class CitationRuleTests(unittest.TestCase):
    def test_a_no_flag_explanation_may_cite_nothing_but_never_the_unretrieved(self) -> None:
        text = "The evidence does not explain this score."
        self.assertFalse(grounding_checks(text, GROUNDING_HITS, "", [])["citations_ok"])
        self.assertTrue(grounding_checks(text, GROUNDING_HITS, "", [],
                                         require_citation=False)["citations_ok"])
        stray = grounding_checks("See [FG-DOC-047].", GROUNDING_HITS, "", [],
                                 require_citation=False)
        self.assertFalse(stray["citations_ok"])


class MarkUnretrievedTests(unittest.TestCase):
    def test_only_unretrieved_citations_are_struck(self) -> None:
        text = "Timeouts recur [FG-DOC-002, FG-DOC-005] and [FG-DOC-005 not provided]."
        marked = mark_unretrieved(text, ["FG-DOC-005"])
        self.assertEqual(
            marked,
            "Timeouts recur [FG-DOC-002, ~~FG-DOC-005~~ *(not retrieved)*] and "
            "[~~FG-DOC-005~~ *(not retrieved)* not provided].",
        )

    def test_a_fully_grounded_text_is_unchanged(self) -> None:
        text = "Kernel rollbacks [FG-DOC-001]."
        self.assertEqual(mark_unretrieved(text, []), text)


class GroundingCheckTests(unittest.TestCase):
    """Each check fires on the defect it names before its silence means anything."""

    def test_clean_text_passes(self) -> None:
        text = "Kernel changes drove 400 rollbacks [FG-DOC-001]. kernel_touched is active at CVSS 7.9."
        checks = grounding_checks(text, GROUNDING_HITS, "Max CVSS: 7.9", ["kernel_touched"])
        self.assertTrue(checks["citations_ok"] and checks["numbers_ok"] and checks["flags_ok"])

    def test_fabricated_number_unretrieved_citation_and_borrowed_flag(self) -> None:
        text = ("Kernel changes drove 73 rollbacks [FG-DOC-001], as policy [FG-DOC-047] warns. "
                "This deployment has bootloader_touched active.")
        checks = grounding_checks(text, GROUNDING_HITS, "Max CVSS: 7.9", ["kernel_touched"])
        self.assertEqual(checks["unretrieved_citations"], ["FG-DOC-047"])
        self.assertEqual(checks["stray_numbers"], ["73"])
        self.assertEqual(checks["borrowed_flags"], ["bootloader_touched"])

    def test_a_number_inside_a_longer_one_is_not_grounded(self) -> None:
        checks = grounding_checks("Seen 61 times [FG-DOC-001].", GROUNDING_HITS, "", [])
        self.assertEqual(checks["stray_numbers"], ["61"])

    def test_evidence_labels_are_references_not_numbers(self) -> None:
        """Llama cited "[Evidence 10: FG-DOC-046]"; the label is the prompt's own."""
        text = "Emergency deployments fail [Evidence 10: FG-DOC-001], see Evidence 3."
        checks = grounding_checks(text, GROUNDING_HITS, "", [])
        self.assertTrue(checks["numbers_ok"], checks)

    def test_a_derived_number_is_still_caught(self) -> None:
        """The same Llama answer computed "approximately 4 versions" (5 - 1);
        the prompt forbids numbers absent from the input, so this must flag."""
        text = "A jump of approximately 4 versions [Evidence 1: FG-DOC-001]."
        checks = grounding_checks(text, GROUNDING_HITS, "Firmware: 1.1.26 -> 5.2.7", [])
        self.assertEqual(checks["stray_numbers"], ["4"])

    def test_historical_wording_does_not_excuse_a_borrowed_flag(self) -> None:
        text = "Like the historical cases, this deployment has bootloader_touched [FG-DOC-001]."
        checks = grounding_checks(text, GROUNDING_HITS, "", ["kernel_touched"])
        self.assertEqual(checks["borrowed_flags"], ["bootloader_touched"])

    def test_the_real_borrowing_from_the_eval_is_caught(self) -> None:
        text = ("Deployments characterized by a major_version_jump, such as the one planned, "
                "frequently degrade [FG-DOC-001].")
        checks = grounding_checks(text, GROUNDING_HITS, "", ["kernel_touched"])
        self.assertEqual(checks["borrowed_flags"], ["major_version_jump"])

    def test_reporting_what_the_evidence_says_is_not_borrowing(self) -> None:
        """Five of the six flag hits in the third eval run were this shape."""
        text = ("Evidence from FG-DOC-001 indicates that the confluence of "
                "protocol_mismatch_flag, high_criticality_site and major_version_jump "
                "creates a high-probability failure state.")
        checks = grounding_checks(text, GROUNDING_HITS, "", ["kernel_touched"])
        self.assertTrue(checks["flags_ok"], checks)


class FlagTests(unittest.TestCase):
    def test_flags_use_the_corpus_definitions(self) -> None:
        raw = make_training_frame(120).drop(
            columns=["deployment_outcome", "time_to_failure_hours", "rollback_required"]
        )
        raw.loc[0, ["past_failure_count", "network_stress_score"]] = [2, 0.66]
        raw.loc[1, ["past_failure_count", "network_stress_score"]] = [3, 0.64]
        raw.loc[0, ["current_firmware", "target_firmware"]] = ["4.0.0", "3.0.0"]  # downgrade
        raw.loc[1, ["current_firmware", "target_firmware"]] = ["3.0.0", "4.0.0"]  # upgrade
        flags = deployment_flags(raw)
        first, second = (flags.loc[raw.loc[i, "deployment_id"], "active_flags"] for i in (0, 1))
        self.assertIn("high_network_stress", first)
        self.assertNotIn("repeat_failure_device", first)
        self.assertNotIn("major_version_jump", first)
        self.assertIn("repeat_failure_device", second)
        self.assertNotIn("high_network_stress", second)
        self.assertIn("major_version_jump", second)

    def test_transition_bands(self) -> None:
        self.assertEqual([transition_class(v) for v in (4.9, 5.0, 7.9, 8.0, None)],
                         [None, "major", "major", "extreme", None])


class EvaluateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = LocalIndex(CHUNKS, CountingEmbedder())
        self.history = pd.DataFrame(
            [_row(name=d) for d in ("DEP_000001", "DEP_000002", "DEP_000003")]
            + [_row(name="DEP_000009", vendor_name="Cisco")]
        )
        self.upcoming = pd.DataFrame([_row(name="UPC_00001"), _row(name="UPC_00002", vendor_name="Moxa")])

    def test_gold_set_drops_citations_whose_row_is_another_vendor(self) -> None:
        gold = gold_queries(RECORDS, self.history)
        self.assertNotIn("DEP_000009", {g["deployment_id"] for g in gold})
        self.assertEqual({g["route"] for g in gold}, {"evidence", "runbook", "transition"})

    def test_report_gates_on_evidence_recall_and_negative_controls(self) -> None:
        report = evaluate(RECORDS, self.index, self.history, self.upcoming)
        self.assertEqual(report["evidence"]["queries"], 1)
        self.assertIn("evidence_recall_at_5", report["gates"])
        self.assertNotIn("runbook", " ".join(report["gates"]))
        controls = report["negative_controls"]
        self.assertTrue(all(c["passed"] and c["generator_calls"] == 0 for c in controls), controls)
        self.assertIn("real out-of-corpus vendor Moxa (UPC_00002)", [c["probe"] for c in controls])
