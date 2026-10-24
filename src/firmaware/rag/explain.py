"""One deployment in, one gated and checked explanation out.

The order is the contract: retrieve, gate, and only then spend a model call.
`insufficient_evidence` and `low_confidence` return without touching the
generator, which is what keeps an unknown vendor from receiving a fluent,
confident and entirely borrowed explanation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..features import _major
from .gate import DEFAULT_GATE, Assessment, GateConfig, assess
from .generate import SYSTEM_PROMPT, Generator, grounding_checks, user_message
from .retrieval import Hit, SearchBackend, retrieve

NO_FLAG_INSTRUCTION = (
    "\n\nNOTE: This deployment raises none of the risk flags the evidence discusses. "
    "The evidence therefore does not explain the model score. Say so plainly, and do not "
    "attribute the score to the evidence."
)


@dataclass
class Explanation:
    deployment_id: str
    assessment: Assessment
    routes: dict[str, list[Hit]]
    context: str
    text: str | None = None
    generated_by: str | None = None
    error: str | None = None
    checks: dict = field(default_factory=dict)

    @property
    def evidence(self) -> list[Hit]:
        seen, hits = set(), []
        for route_hits in self.routes.values():
            for hit in route_hits:
                if hit.chunk["chunk_id"] not in seen:
                    seen.add(hit.chunk["chunk_id"])
                    hits.append(hit)
        return hits


def context_block(row: pd.Series, flags: list[str]) -> str:
    """The deployment as the writer may describe it.

    Every figure the evidence reasons about has its counterpart here. The first
    generation eval found the writer comparing deployments with historical
    averages ("exceeds the historical 2.37") while this block omitted the
    deployment's own jump magnitude and maintenance window, so it inferred them
    and the comparisons could not be checked.
    """

    def value(column: str) -> object:
        item = row.get(column)
        return "unknown" if item is None or (isinstance(item, float) and math.isnan(item)) else item

    def major(column: str) -> object:
        parsed = _major(row.get(column))
        return "unknown" if parsed is None else parsed

    window = value("maintenance_window")
    lines = [
        f"Vendor: {row['vendor_name']}",
        f"Device type: {row['device_type']}",
        f"Fleet tier: {value('fleet_tier')}",
        f"Firmware: {row['current_firmware']} -> {row['target_firmware']}",
        # Both lines, and the label, because the writer conflated them: given
        # only the magnitude beside an active major_version_jump, it explained
        # the flag by the magnitude ("triggered because the magnitude is 1.0")
        # and once read 4.0 -> 4.2 at magnitude 4.42 as a major jump. The
        # corpus compares magnitudes with historical averages; the flag is
        # only ever the major number going up.
        f"Major version: {major('current_firmware')} -> {major('target_firmware')}",
        f"Version jump magnitude: {value('version_jump_magnitude')} (not the major_version_jump flag)",
        f"Deployment type: {row['deployment_type']}",
        f"Maintenance window: {window if window == 'unknown' else ('yes' if window else 'no')}",
        f"Site criticality: {row['site_criticality']}",
        f"Max CVSS: {row['max_cvss_score']}",
        f"Past failures on this device: {value('past_failure_count')}",
    ]
    probability = row.get("risk_probability")
    if isinstance(probability, float) and not math.isnan(probability):
        lines.append(
            f"Model score: {row.get('risk_band', '?')} / {row.get('risk_prediction', '?')} "
            f"(probability {probability:.4f}, about {probability:.0%})"
        )
    lines.append(f"Active Risk Flags: {', '.join(flags) or 'none'}")
    return "\n".join(lines)


def explain(
    row: pd.Series,
    records: list[dict],
    backend: SearchBackend,
    generator: Generator | None = None,
    config: GateConfig = DEFAULT_GATE,
) -> Explanation:
    """`row` is a `deployment_flags` row, optionally joined with its score."""
    flags = list(row["active_flags"])
    routes = retrieve(row, flags, records, backend)
    assessment = assess(routes["evidence"], row["vendor_name"], flags, config)
    result = Explanation(str(row.name), assessment, routes, context_block(row, flags))

    if assessment.verdict == "insufficient_evidence":
        result.text = (
            f"No {row['vendor_name']} evidence exists in the corpus. Documents about other "
            "vendors do not apply, and similarity is not relevance. Refer this deployment "
            "to an engineer familiar with the vendor."
        )
        return result
    if assessment.verdict == "low_confidence":
        result.text = "Low confidence, not explained: " + "; ".join(assessment.reasons)
        return result
    if generator is None:
        return result

    message = user_message(result.context, result.evidence)
    if assessment.verdict == "no_flag_evidence":
        # Half the batch lands here: a score the evidence cannot account for,
        # sent to a prompt that asks why the deployment is risky. Without this,
        # the likeliest output is a plausible, invented cause.
        message += NO_FLAG_INSTRUCTION
    generation = generator.generate(SYSTEM_PROMPT, message)
    if generation.text is None:
        # Kept apart from text: a failed call is not an explanation, and a page
        # that rendered it as one would present an error as the analyst's words.
        result.error = f"{generator.name}: {generation.stop_reason}"
        return result
    result.generated_by = generator.name
    result.text = generation.text
    result.checks = grounding_checks(
        generation.text, result.evidence, result.context, flags,
        require_citation=assessment.verdict != "no_flag_evidence",
    )
    return result
