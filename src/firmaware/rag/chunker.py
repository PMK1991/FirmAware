"""Split each document type along the structure it deliberately carries.

One generic splitter destroys that structure: a halved table is meaningless, an
isolated runbook step is unusable, and a rule split from its exception clause is
misleading, which is worse than missing. So every type has its own strategy, and
`check_invariants` states what each strategy promises in a form tests assert.

PDFs arrive as flat text: headings lose their `##`, tables lose their pipes.
A strategy that finds none of the structure it splits on returns the document
as one `whole` chunk rather than guessing, and `structure_warnings` reports it.
Silently accepting that collapse is the failure this module exists to prevent;
converting the PDFs away would only hide it.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from .corpus import document_files, load_manifest, read_text

SEPARATOR = re.compile(r"^\s*-{3,}\s*$")
ITALIC_LINE = re.compile(r"^\s*([*_])[^*_].*\1\s*$")
# Boilerplate a PDF repeats at every page end; in markdown it is the italic
# footer, which is dropped the same way.
PDF_FOOTER = re.compile(
    r"^(FirmGuard AI RAG Knowledge Base|Source: \d+ historical|"
    r"Do not reference this document|Policy informed by|Vectorless retrieval target)"
)
STEP = re.compile(r"^\s*(\d+)\.\s+")
TABLE_ROW = re.compile(r"^\s*\|")
TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
SECTION = "## "

RISKNOTE_TOKENS = (300, 700)
STEPS_PER_CHUNK = 4
# A document whose chunk count is this far from its type's median is reported.
DEVIATION = 0.5


def tokens(text: str) -> int:
    """The HLD's token estimate. Crude, but stable and dependency-free."""
    return int(len(text.split()) * 1.3)


def _clean(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    for index in range(len(lines) - 1, -1, -1):
        if SEPARATOR.match(lines[index]):
            tail = [line for line in lines[index + 1 :] if line.strip()]
            if tail and all(ITALIC_LINE.match(line) for line in tail):
                lines = lines[:index]
            break
    return [
        line
        for line in lines
        if not SEPARATOR.match(line) and not PDF_FOOTER.match(line.strip())
    ]


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _sections(lines: list[str]) -> list[list[str]]:
    """Split at `## ` headings; heading-only sections fold into the next.

    A title line with nothing under it is not a section. Folding it forward
    keeps the title with the metadata that follows instead of emitting a chunk
    that is only a title.
    """
    raw: list[list[str]] = [[]]
    for line in lines:
        if line.startswith(SECTION) and raw[-1]:
            raw.append([])
        raw[-1].append(line)

    sections: list[list[str]] = []
    carry: list[str] = []
    for section in raw:
        section = carry + section
        if any(line.strip() and not _is_heading(line) for line in section):
            sections.append(section)
            carry = []
        else:
            carry = section
    if carry:
        if sections:
            sections[-1] += carry
        else:
            sections.append(carry)
    return sections


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            blocks[-1].append(line)
        elif blocks[-1]:
            blocks.append([])
    return [block for block in blocks if block]


def chunk_by_heading(text: str) -> list[tuple[str, str]]:
    sections = _sections(_clean(text))
    if not any(line.startswith(SECTION) for section in sections[1:] for line in section):
        return [(_join(_clean(text)), "whole")]
    return [(_join(section), "prose") for section in sections]


def chunk_advisory(text: str) -> list[tuple[str, str]]:
    """Tables atomic, prose by paragraph, each carrying its section heading."""
    sections = _sections(_clean(text))
    if len(sections) < 2:
        return [(_join(_clean(text)), "whole")]
    chunks = []
    for section in sections:
        split = next(
            (i for i, line in enumerate(section) if line.strip() and not _is_heading(line)),
            len(section),
        )
        heading, body = _join(section[:split]), section[split:]
        for block in _blocks(body):
            # A block can hold prose and a table with no blank line between;
            # split it at the table's edges so the table stays whole.
            runs: list[list[str]] = []
            for line in block:
                if runs and bool(TABLE_ROW.match(runs[-1][0])) == bool(TABLE_ROW.match(line)):
                    runs[-1].append(line)
                else:
                    runs.append([line])
            for run in runs:
                kind = "table" if TABLE_ROW.match(run[0]) else "prose"
                chunks.append((_join([heading, "", *run]), kind))
    return chunks


def chunk_risknote(text: str) -> list[tuple[str, str]]:
    """Paragraph groups balanced to fit 300-700 tokens.

    Greedy filling to 700 leaves a runt last group; dividing the note into the
    fewest groups that fit and aiming each at an equal share does not.
    """
    paragraphs = [_join(block) for block in _blocks(_clean(text))]
    sizes = [tokens(p) for p in paragraphs]
    groups = max(1, math.ceil(sum(sizes) / RISKNOTE_TOKENS[1]))
    share = sum(sizes) / groups
    chunks: list[list[str]] = [[]]
    done = 0
    for paragraph, size in zip(paragraphs, sizes):
        if chunks[-1] and len(chunks) < groups and done + size / 2 > share * len(chunks):
            chunks.append([])
        chunks[-1].append(paragraph)
        done += size
    return [("\n\n".join(group), "prose") for group in chunks]


def _step_groups(count: int) -> list[int]:
    """Sizes of at most four, as even as possible, so no group is a lone step."""
    groups = max(1, math.ceil(count / STEPS_PER_CHUNK))
    base, extra = divmod(count, groups)
    return [base + (1 if i < extra else 0) for i in range(groups)]


def chunk_runbook(text: str) -> list[tuple[str, str]]:
    """Numbered steps in groups of up to four, each under its section heading.

    Runs are found by their numbering restarting at 1 rather than by `##`, so
    a PDF runbook, whose numbering survives extraction, keeps its structure.
    A PDF wraps long steps onto unnumbered lines; those are rejoined only while
    the step is mid-sentence, and never the line directly above a `1.`, which
    is the next section's heading. Without the sentence test, a table that
    follows the last step is read as that step's continuation.
    """
    lines = [line for line in _clean(text) if line.strip()]
    items: list[list[Any]] = []  # [step number or None, text]
    for index, line in enumerate(lines):
        match = STEP.match(line)
        following = STEP.match(lines[index + 1]) if index + 1 < len(lines) else None
        heads_next_run = following is not None and following.group(1) == "1"
        if match:
            items.append([int(match.group(1)), line.strip()])
        elif (
            items
            and items[-1][0] is not None
            and not items[-1][1].endswith((".", ":", "!", "?"))
            and not heads_next_run
            and not line.lstrip().startswith(("#", ">", "*", "|", "-"))
        ):
            items[-1][1] += " " + line.strip()
        else:
            items.append([None, line])

    runs: list[tuple[str, list[str]]] = []
    prose: list[list[str]] = [[]]
    for index, (number, line) in enumerate(items):
        if number is None:
            prose[-1].append(line)
            continue
        if number == 1 or not runs or runs[-1][1] == [] or number != len(runs[-1][1]) + 1:
            heading = prose[-1].pop() if prose[-1] else ""
            runs.append((heading, []))
            prose.append([])
        runs[-1][1].append(line)

    if not runs:
        return [(_join(_clean(text)), "whole")]
    chunks = [(_join(block), "prose") for block in prose[:1] if block]
    for (heading, steps), after in zip(runs, prose[1:]):
        start = 0
        for size in _step_groups(len(steps)):
            chunks.append((_join([heading, *steps[start : start + size]]), "step"))
            start += size
        if after:
            chunks.append((_join(after), "prose"))
    return chunks


def chunk_policy(text: str) -> list[tuple[str, str]]:
    """One rule per chunk, carrying the policy's exception process with it.

    These policies state their exceptions once, in a section that governs
    every rule, not beside each rule. A rule chunk retrieved without it reads
    as unconditional, so the exception text travels with every rule.
    """
    lines = _clean(text)
    sections = _sections(lines)
    title = next((line for line in lines if line.startswith("# ")), "")

    def find(name: str) -> list[str] | None:
        return next((s for s in sections if s[0].startswith(SECTION + name)), None)

    rules, exception = find("Mandatory Rules"), find("Exception Process")
    if rules is None or exception is None:
        return [(_join(lines), "whole")]
    chunks = []
    for section in sections:
        if section is exception:
            continue
        if section is not rules:
            chunks.append((_join(section), "prose"))
            continue
        for rule in (line for line in section if STEP.match(line)):
            chunks.append(
                (_join([title, rules[0], rule, "", *exception]), "rule")
            )
    return chunks


def chunk_whole(text: str) -> list[tuple[str, str]]:
    return [(_join(_clean(text)), "whole")]


# The taxonomy is open, the dispatch is fixed: a new type registers here.
STRATEGIES = {
    "Postmortem": chunk_by_heading,
    "Advisory": chunk_advisory,
    "RiskNote": chunk_risknote,
    "Runbook": chunk_runbook,
    "Policy": chunk_policy,
    "Matrix": chunk_whole,
    "Transition": chunk_by_heading,
    "CrossVendor": chunk_by_heading,
    "META": chunk_whole,
}
WHOLE_BY_DESIGN = {"Matrix", "META"}


def chunk_document(record: dict[str, Any], text: str) -> list[dict[str, Any]]:
    pieces = STRATEGIES[record["document_type"]](text)
    return [
        {
            "chunk_id": f"{record['doc_id']}_chunk_{index}",
            "doc_id": record["doc_id"],
            "chunk_index": index,
            "chunk_kind": kind,
            "document_type": record["document_type"],
            "retrieval_mode": record["retrieval_mode"],
            "vendor": record["vendor"],
            # The HLD's record plus the vendor list: the vendor-match gate has
            # to tell a cross-vendor pair from a vendor-neutral policy.
            "vendors": record["vendors"],
            "evidence_strength": record["evidence_strength"],
            "provenance": record["provenance"],
            "source_format": record["source_format"],
            "filename": record["filename"],
            "text": piece,
        }
        for index, (piece, kind) in enumerate(pieces)
    ]


def chunk_corpus(corpus_dir: Path) -> list[dict[str, Any]]:
    records = {r["filename"]: r for r in load_manifest(corpus_dir)["documents"]}
    chunks = []
    for path in document_files(corpus_dir):
        chunks += chunk_document(records[path.name], read_text(path))
    return chunks


def structure_warnings(chunks: list[dict[str, Any]]) -> list[str]:
    """Documents whose structure did not survive, named rather than accepted.

    The median is taken over markdown documents only. Half the advisories are
    PDFs that collapse to one chunk, so a median over every format would sit
    between the two populations and flag both.
    """
    per_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        per_doc[chunk["doc_id"]].append(chunk)
    by_type: dict[str, list[int]] = defaultdict(list)
    for doc_chunks in per_doc.values():
        if doc_chunks[0]["source_format"] == "md":
            by_type[doc_chunks[0]["document_type"]].append(len(doc_chunks))

    warnings = []
    for doc_id, doc_chunks in sorted(per_doc.items()):
        first = doc_chunks[0]
        document_type = first["document_type"]
        if document_type in WHOLE_BY_DESIGN:
            continue
        median = statistics.median(by_type[document_type]) if by_type[document_type] else None
        collapsed = any(c["chunk_kind"] == "whole" for c in doc_chunks)
        deviates = median and abs(len(doc_chunks) - median) / median > DEVIATION
        if collapsed or deviates:
            warnings.append(
                f"{doc_id} ({first['filename']}): {len(doc_chunks)} chunk(s) against "
                f"a {document_type} median of {median}"
                + ("; structure lost in extraction, kept whole" if collapsed else "")
            )
    return warnings


def _table_runs(text: str) -> list[list[str]]:
    runs: list[list[str]] = []
    previous = False
    for line in text.split("\n"):
        row = bool(TABLE_ROW.match(line))
        if row and previous:
            runs[-1].append(line)
        elif row:
            runs.append([line])
        previous = row
    return runs


def check_invariants(chunks: list[dict[str, Any]]) -> list[str]:
    """Every promise the strategies make, as violations. Empty means kept.

    `whole` chunks are exempt from the per-type rules: they are the documents
    whose structure was lost, which `structure_warnings` reports instead.
    """
    problems = []
    per_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        per_doc[chunk["doc_id"]].append(chunk)
    ids = [chunk["chunk_id"] for chunk in chunks]
    if len(ids) != len(set(ids)):
        problems.append("chunk ids are not unique")

    for doc_id, doc_chunks in per_doc.items():
        document_type = doc_chunks[0]["document_type"]
        if [c["chunk_index"] for c in doc_chunks] != list(range(len(doc_chunks))):
            problems.append(f"{doc_id}: chunk indexes are not contiguous from 0")
        if document_type in WHOLE_BY_DESIGN and len(doc_chunks) != 1:
            problems.append(f"{doc_id}: {document_type} must be exactly one chunk")

        for chunk in doc_chunks:
            where = chunk["chunk_id"]
            text, kind = chunk["text"], chunk["chunk_kind"]
            if not text.strip():
                problems.append(f"{where}: empty")
            if kind == "whole":
                continue
            lines = text.split("\n")
            steps = [int(m.group(1)) for m in map(STEP.match, lines) if m]

            if document_type in {"Postmortem", "Transition", "CrossVendor"}:
                if not _is_heading(lines[0]):
                    problems.append(f"{where}: does not begin at a heading")
                if sum(line.startswith(SECTION) for line in lines) != 1:
                    problems.append(f"{where}: merges sections")
            if document_type == "CrossVendor":
                named = {v for v in ("Cisco", "Honeywell", "Schneider", "Siemens") if v in text}
                if not named <= set(chunk["vendors"]):
                    problems.append(f"{where}: names vendors outside its pair {sorted(named)}")
            if document_type == "Advisory":
                for run in _table_runs(text):
                    if len(run) < 2 or not TABLE_RULE.match(run[1]):
                        problems.append(f"{where}: table rows cut from their header")
                if kind == "prose" and _table_runs(text):
                    problems.append(f"{where}: prose chunk carries table rows")
            if document_type == "RiskNote":
                low, high = RISKNOTE_TOKENS
                if not low <= tokens(text) <= high:
                    problems.append(f"{where}: {tokens(text)} tokens, outside {low}-{high}")
            if kind == "step":
                if len(steps) < 2:
                    problems.append(f"{where}: fewer than two steps")
                if steps and steps != list(range(steps[0], steps[0] + len(steps))):
                    problems.append(f"{where}: step numbering is not contiguous")
            if kind == "rule":
                if len(steps) != 1:
                    problems.append(f"{where}: {len(steps)} rules in one chunk")
                if "Exception Process" not in text:
                    problems.append(f"{where}: rule without its exception clause")
    return problems
