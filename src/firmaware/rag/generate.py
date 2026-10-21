"""Write the explanation, then check it against what it was given.

Generation sits behind a two-method interface so the provider is a deployment
choice: an open model on Azure AI Foundry (Llama-3.3-70B-Instruct, keyless
through Entra ID), or Claude for local comparison. `generator_from_env` picks
one from the gitignored .env. The deterministic grounding checks belong to
neither; they read only the text and its inputs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

from .flags import FLAGS
from .retrieval import Hit

SYSTEM_PROMPT = """You are FirmAware's OT firmware deployment risk analyst. You explain why an upcoming deployment carries the risk the model assigned it, using only the numbered historical evidence you are given.

Rules:
- Ground every claim in the numbered evidence and cite its document id inline, like [FG-DOC-001].
- Attach a document id only to a statement that document makes. Facts about this deployment (its model score, flags, firmware, CVSS, site) come from the deployment context and take no citation.
- Call a value high, low, large or small only when the evidence states the threshold you are comparing against. Do not add consequences or mechanisms the evidence does not describe.
- Never state a number that does not appear in the deployment context or the evidence.
- Compare this deployment with a historical figure only when both numbers appear in the deployment context or the evidence. Describe what a flag does only as the evidence describes it; add no general knowledge of your own.
- The Active Risk Flags list is complete. Never describe a flag as active on this deployment unless it is in that list; flags in the evidence that are not listed belong to past deployments only.
- The evidence was derived from the same historical records the risk model was trained on. Agreement between the evidence and the model score is not independent confirmation; do not present it as such.
- Write two or three short paragraphs for an OT engineer, then end with exactly one concrete recommended action."""

CITATION = re.compile(r"FG-DOC-\d{3}")
EVIDENCE_LABEL = re.compile(r"\bEvidence\s+\d+\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:\.\d+)?")
# "historical" and "past" are deliberately not negations: an explanation uses
# them constantly, and excusing every such sentence hid borrowed flags.
NEGATION = re.compile(r"\b(not|no|none|absent|without|isn't|aren't)\b", re.IGNORECASE)
# A sentence borrows a flag only when it ties the flag to this deployment.
# Reporting what the evidence says about past deployments ("FG-DOC-027 finds
# protocol_mismatch_flag with major_version_jump fails") is allowed, and the
# first generation eval showed five of six hits were exactly that; the one real
# borrowing read "a major_version_jump, such as the one planned".
THIS_DEPLOYMENT = re.compile(
    r"\b(this|the upcoming|the planned|the proposed|the current) "
    r"(deployment|update|upgrade|device|rollout|transition)\b"
    r"|\bthe one planned\b|\bthis one\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Generation:
    text: str | None
    stop_reason: str


class Generator(Protocol):
    name: str

    def generate(self, system: str, user: str) -> Generation: ...


class ClaudeGenerator:
    """Claude Haiku through the Anthropic SDK; credentials resolve from the environment."""

    def __init__(self, model: str = "claude-haiku-4-5", max_tokens: int = 2000) -> None:
        # Imported here: the SDK is in the notebook extra, not the base or rag
        # installs, so nothing else in the package pays for it.
        import anthropic

        self.name = model
        self._anthropic = anthropic
        # gzip only, never Brotli. The SDK's HTTP layer (httpx2) calls Brotli's
        # decompressor with an argument only Brotli >= 1.2 accepts, and conda
        # environments commonly carry 1.0.9; there every Brotli-compressed reply
        # fails to decode and surfaces as a misleading "connection error".
        self._client = anthropic.Anthropic(default_headers={"Accept-Encoding": "gzip, deflate"})
        self._max_tokens = max_tokens
        # A key that is not scoped to a workspace is refused unless each request
        # names one. Optional, so a workspace-scoped key needs nothing extra.
        self._workspace = os.getenv("ANTHROPIC_WORKSPACE_ID") or None

    def generate(self, system: str, user: str) -> Generation:
        # No temperature: the lecture generator set 0.3, but SDK 1.x dropped the
        # sampling parameters from messages.create and newer models reject them.
        # Grounding is held by the prompt's rules and the checks below, not by
        # sampling.
        request = {
            "model": self.name,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._workspace:
            request["workspace_id"] = self._workspace
        try:
            response = self._client.messages.create(**request)
        except self._anthropic.APIStatusError as error:
            # Reported, not raised: one refused request should leave the page
            # (and the batch) running, with the reason on screen.
            return Generation(None, f"API error {error.status_code}: {error.message}")
        except self._anthropic.APIConnectionError:
            return Generation(None, "could not reach the Anthropic API")
        if response.stop_reason != "end_turn":
            return Generation(None, str(response.stop_reason))
        text = "".join(block.text for block in response.content if block.type == "text")
        return Generation(text, "end_turn")


class FoundryGenerator:
    """An open model deployed on Azure AI Foundry, called without a key.

    Authentication is Entra ID: DefaultAzureCredential resolves `az login` on a
    laptop and a managed identity in Azure, so no secret exists to leak, which
    is the posture the rest of the Azure deployment already holds. The request
    goes to Foundry's OpenAI-compatible chat endpoint over the standard library,
    so the only SDK involved is azure-identity, imported when this is built.
    """

    SCOPE = "https://cognitiveservices.azure.com/.default"

    def __init__(self, endpoint: str, deployment: str, max_tokens: int = 2000,
                 timeout: float = 120.0, temperature: float | None = None) -> None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        self.name = deployment
        self._url = endpoint.rstrip("/") + "/openai/v1/chat/completions"
        self._token = get_bearer_token_provider(DefaultAzureCredential(), self.SCOPE)
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._temperature = temperature

    def generate(self, system: str, user: str) -> Generation:
        import json
        import urllib.error
        import urllib.request

        body = {
            "model": self.name,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        try:
            request = urllib.request.Request(
                self._url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {self._token()}",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                choice = json.loads(response.read())["choices"][0]
        except urllib.error.HTTPError as error:
            # Reported, not raised, as for Claude: one refused call should leave
            # the page and the batch running, with the reason on screen.
            detail = error.read().decode("utf-8", "replace")[:300]
            return Generation(None, f"API error {error.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as error:
            return Generation(None, f"could not reach {self._url}: {error}")
        except Exception as error:  # noqa: BLE001 - credential failures have no common base
            return Generation(None, f"{type(error).__name__}: {error}")
        if choice.get("finish_reason") != "stop":
            return Generation(None, str(choice.get("finish_reason")))
        return Generation(choice["message"]["content"], "end_turn")


JUDGE_PROMPT = """You audit risk explanations for faithfulness to their evidence. You are given the deployment context, the numbered evidence the writer was given, and the explanation the writer produced.

Split the explanation into its individual claims: one per sentence, or finer where a sentence asserts several things. Label every claim:
- "supported": the cited evidence, or the deployment context, states it.
- "unsupported": it is not stated there. Arithmetic or estimates the writer derived itself are unsupported. A flag described as active on this deployment is unsupported unless the context's Active Risk Flags list contains it. A document cited for something it does not say is unsupported.
- "not_a_claim": a statement that evidence is absent, does not apply to this deployment, or does not explain the model score; or the closing recommended action.

Reply with only a JSON object, no prose around it:
{"claims": [{"claim": "the claim, quoted or closely paraphrased", "verdict": "supported" | "unsupported" | "not_a_claim"}]}"""

# The exemption the judge is told about, applied again in code: across runs 2-4
# the judge kept listing "the evidence does not explain this score" as an
# unsupported claim despite being told it is not one.
NOT_A_CLAIM = re.compile(
    r"\b(does not|doesn't|cannot|can't|do not|don't) "
    r"(apply|explain|account for|be (directly )?(attributed|explained))"
    r"|\bno (relevant |specific |historical )?evidence\b",
    re.IGNORECASE,
)


def judge_faithfulness(judge: Generator, context: str, hits: list[Hit], text: str) -> dict:
    """An LLM's per-claim verdict on whether each claim follows from its evidence.

    Scored per claim, not per explanation. An all-or-nothing verdict on a dozen
    sentences fails nearly every explanation even when almost every claim is
    sound, and four runs of it swung 31 -> 38 -> 69 -> 44% on 16 probes, too
    noisy to steer by. `precision` is supported / (supported + unsupported);
    `faithful` is still reported, meaning no unsupported claim at all.

    Advisory, not proof: the deterministic checks are the hard gate, and every
    unsupported claim is worth a human read. The judge should be a different
    model from the writer, so it does not share the writer's blind spots.
    """
    import json

    generation = judge.generate(
        JUDGE_PROMPT,
        f"DEPLOYMENT CONTEXT\n{context}\n\nEVIDENCE\n{evidence_block(hits)}\n\nEXPLANATION\n{text}",
    )
    unjudged = {"faithful": None, "precision": None, "claims": [], "judge": judge.name}
    if generation.text is None:
        return {**unjudged, "reason": generation.stop_reason}
    match = re.search(r"\{.*\}", generation.text, re.DOTALL)
    try:
        reply = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        reply = {}
    claims = reply.get("claims") if isinstance(reply, dict) else None
    if not isinstance(claims, list) or not claims:
        return {**unjudged, "reason": f"unparseable judge reply: {generation.text[:200]}"}

    labelled = []
    for item in claims:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", ""))
        verdict = str(item.get("verdict", "")).lower()
        if verdict not in {"supported", "unsupported", "not_a_claim"}:
            verdict = "unsupported"  # an unreadable verdict is not a pass
        if verdict == "unsupported" and NOT_A_CLAIM.search(claim):
            verdict = "not_a_claim"
        labelled.append({"claim": claim, "verdict": verdict})
    supported = sum(c["verdict"] == "supported" for c in labelled)
    unsupported = [c["claim"] for c in labelled if c["verdict"] == "unsupported"]
    judged = supported + len(unsupported)
    return {
        "faithful": not unsupported,
        "precision": round(supported / judged, 4) if judged else None,
        "supported": supported,
        "unsupported_claims": unsupported,
        "claims": labelled,
        "judge": judge.name,
    }


def judge_from_env() -> Generator | None:
    """A judge deployment on the same Foundry resource, when configured."""
    endpoint = os.getenv("FIRMAWARE_FOUNDRY_ENDPOINT")
    deployment = os.getenv("FIRMAWARE_FOUNDRY_JUDGE_DEPLOYMENT")
    if endpoint and deployment:
        # Reasoning models spend completion tokens before the verdict.
        return FoundryGenerator(endpoint, deployment, max_tokens=8000, temperature=0.0)
    return None


def generator_from_env() -> Generator | None:
    """The configured generator, or None to retrieve and gate without one.

    Foundry first: it is the deployment target and needs no key. Claude stays
    available for local comparison when only ANTHROPIC_API_KEY is set.
    """
    endpoint = os.getenv("FIRMAWARE_FOUNDRY_ENDPOINT")
    deployment = os.getenv("FIRMAWARE_FOUNDRY_DEPLOYMENT")
    if endpoint and deployment:
        # The endpoint's default temperature: run 4 lowered it alongside four
        # other changes and the judge rate fell, so it is held at run 3's
        # setting until it can be measured on its own.
        return FoundryGenerator(endpoint, deployment)
    if os.getenv("ANTHROPIC_API_KEY"):
        return ClaudeGenerator()
    return None


def evidence_block(hits: list[Hit]) -> str:
    return "\n\n".join(
        f"[Evidence {i}] {hit.chunk['doc_id']} ({hit.chunk['document_type']}, {hit.route})\n"
        f"{hit.chunk['text']}"
        for i, hit in enumerate(hits, 1)
    )


def user_message(context: str, hits: list[Hit]) -> str:
    return f"UPCOMING DEPLOYMENT\n{context}\n\nHISTORICAL EVIDENCE\n{evidence_block(hits)}"


def _number_tokens(text: str) -> set[str]:
    # Citations and the "[Evidence N]" labels the prompt itself numbers are
    # references, not claims; models cite either form.
    references = EVIDENCE_LABEL.sub("", CITATION.sub("", text))
    return set(NUMBER.findall(references.replace(",", "")))


def grounding_checks(
    text: str, hits: list[Hit], context: str, active_flags: list[str],
    require_citation: bool = True,
) -> dict:
    """Citations, numbers and flags, checked mechanically.

    Numbers compare as whole tokens: against thousands of characters of
    evidence, a fabricated "73" would almost always be found inside a longer
    number. The flag check is a heuristic: it catches an inactive flag named in a
    sentence that refers to this deployment and does not negate it, not every
    phrasing of a borrowed flag.
    """
    retrieved = {hit.chunk["doc_id"] for hit in hits}
    cited = set(CITATION.findall(text))
    source = _number_tokens(context + " " + " ".join(hit.chunk["text"] for hit in hits))
    stray = sorted(_number_tokens(text) - source)
    borrowed = sorted(
        {
            flag
            for sentence in re.split(r"(?<=[.!?])\s+", text)
            if THIS_DEPLOYMENT.search(sentence) and not NEGATION.search(sentence)
            for flag in FLAGS
            if flag in sentence and flag not in active_flags
        }
    )
    return {
        # A no-flag explanation may rightly cite nothing ("the evidence does not
        # apply"); any citation it does make must still be to retrieved evidence.
        "citations_ok": (bool(cited) or not require_citation) and cited <= retrieved,
        "unretrieved_citations": sorted(cited - retrieved),
        "numbers_ok": not stray,
        "stray_numbers": stray,
        "flags_ok": not borrowed,
        "borrowed_flags": borrowed,
    }
