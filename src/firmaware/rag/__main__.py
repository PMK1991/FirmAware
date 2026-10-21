"""Corpus commands: `manifest` derives the contract, `lint` holds the corpus to
it, and `chunk` splits it and checks every chunking invariant. `eval` grades
retrieval and the gate; `explain` runs one deployment end to end.

`eval` and `explain` need what CI does not have: the ignored deployment CSVs
and the BGE model from the notebook extra. When either is missing they exit 2,
"not run", which is deliberately neither a pass nor a failure."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import pandas as pd

from .chunker import check_invariants, chunk_corpus, structure_warnings
from .corpus import (
    build_manifest,
    gold_consistency,
    lint,
    load_manifest,
    write_manifest,
)

DEFAULT_CORPUS = Path("data") / "corpus"
DEFAULT_EVENTS = Path("data") / "deployment_events.csv"
DEFAULT_UPCOMING = Path("data") / "upcoming_deployments.csv"
DEFAULT_SCORES = Path("demo") / "scores.csv"
DEFAULT_REPORT = Path("outputs") / "rag_eval.json"
NOT_RUN = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m firmaware.rag")
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    manifest.add_argument(
        "--source", required=True, help="where the documents were copied from"
    )

    lint_parser = commands.add_parser("lint")
    lint_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    lint_parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)

    generation_parser = commands.add_parser(
        "eval-generation", help="explain a fixed probe set with the real model and grade it"
    )
    generation_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    generation_parser.add_argument("--upcoming", type=Path, default=DEFAULT_UPCOMING)
    generation_parser.add_argument("--scores", type=Path, default=Path("outputs") / "scores.csv")
    generation_parser.add_argument(
        "--out", type=Path, default=Path("outputs") / "rag_generation_eval.json"
    )
    generation_parser.add_argument(
        "--rejudge", type=Path,
        help="re-grade a saved report's explanations with the current judge; no writer calls",
    )
    generation_parser.add_argument(
        "--repeat", type=int, default=3, help="judge passes per text with --rejudge"
    )

    evaluate_parser = commands.add_parser("eval")
    evaluate_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    evaluate_parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    evaluate_parser.add_argument("--upcoming", type=Path, default=DEFAULT_UPCOMING)
    evaluate_parser.add_argument("--scores", type=Path, default=DEFAULT_SCORES)
    evaluate_parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)

    explain_parser = commands.add_parser("explain")
    explain_parser.add_argument("deployment_id")
    explain_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    explain_parser.add_argument("--upcoming", type=Path, default=DEFAULT_UPCOMING)
    explain_parser.add_argument("--scores", type=Path, default=DEFAULT_SCORES)
    explain_parser.add_argument(
        "--no-generate", action="store_true", help="retrieve and gate only"
    )

    index_parser = commands.add_parser(
        "index", help="build an Azure AI Search index; promote it only if the eval passes"
    )
    index_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    index_parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    index_parser.add_argument("--upcoming", type=Path, default=DEFAULT_UPCOMING)
    index_parser.add_argument("--scores", type=Path, default=DEFAULT_SCORES)
    index_parser.add_argument(
        "--keep", type=int, default=2, help="indexes to keep (the Free tier holds 3)"
    )
    index_parser.add_argument("--promote-without-eval", action="store_true")

    chunk = commands.add_parser("chunk")
    chunk.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    chunk.add_argument("--out", type=Path, help="write the chunks as JSON lines")
    return parser


def _print_gold_report(corpus: Path, events: pd.DataFrame) -> None:
    report = pd.DataFrame(gold_consistency(load_manifest(corpus), events))
    by_type = report.groupby("document_type").agg(
        documents=("doc_id", "size"),
        cited_per_doc=("cited", "mean"),
        docs_with_no_ids=("resolved", lambda s: int((s == 0).sum())),
        vendor_match=("vendor_match", "mean"),
        family_match=("family_match", "mean"),
    )
    print("[lint] gold-set consistency by document type")
    print(by_type.round(2).to_string())


def _chunk(corpus: Path, out: Path | None) -> int:
    chunks = chunk_corpus(corpus)
    for document_type, count in sorted(Counter(c["document_type"] for c in chunks).items()):
        print(f"[chunk] {document_type:<12} {count}")
    print(f"[chunk] {len(chunks)} chunks")
    # Reported, not fatal: a collapsed PDF is a known, visible loss. A broken
    # invariant is a chunker defect, and that is what fails the command.
    for warning in structure_warnings(chunks):
        print(f"[chunk] warning: {warning}")
    problems = check_invariants(chunks)
    for problem in problems:
        print(f"[chunk] invariant: {problem}")
    if out is not None:
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            for chunk in chunks:
                handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        print(f"[chunk] wrote {out}")
    return 1 if problems else 0


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(".env"))


def _search_configured() -> bool:
    return all(
        os.getenv(name)
        for name in ("FIRMAWARE_SEARCH_ENDPOINT", "FIRMAWARE_FOUNDRY_ENDPOINT",
                     "FIRMAWARE_FOUNDRY_EMBEDDING_DEPLOYMENT")
    )


def _missing(*paths: Path) -> str | None:
    absent = [str(path) for path in paths if not path.exists()]
    if absent:
        return f"missing {', '.join(absent)}"
    if _search_configured():
        return None  # Azure AI Search serves the index; no local model needed
    try:
        import sentence_transformers  # noqa: F401  (notebook extra; pulls in torch)
    except ImportError:
        return "sentence-transformers is not installed (pip install -e \".[notebooks]\")"
    return None


def _backend(corpus: Path):
    """Azure AI Search when .env configures it, else the local BGE index."""
    from .azure_search import backend_from_env

    backend = backend_from_env()
    if backend is not None:
        print(f"[rag] backend: Azure AI Search {backend.endpoint} / {backend.index}")
        return backend
    from .retrieval import LocalIndex, bge_embedder

    print("[rag] backend: local BGE-small index")
    return LocalIndex(chunk_corpus(corpus), bge_embedder())


def _upcoming(upcoming_path: Path, scores_path: Path) -> pd.DataFrame:
    """Upcoming deployments' flags, joined with the newest scoring run.

    Local `predict` appends every run to one CSV, so only the newest run's
    rows describe what the page shows.
    """
    from .flags import deployment_flags

    upcoming = deployment_flags(pd.read_csv(upcoming_path))
    if scores_path.exists():
        scores = pd.read_csv(scores_path)
        if "scored_at" in scores.columns:
            scores = scores[scores["scored_at"] == scores["scored_at"].max()]
        upcoming = upcoming.join(
            scores.set_index("deployment_id")[["risk_probability", "risk_prediction", "risk_band"]]
        )
    return upcoming


def _runtime(corpus: Path, upcoming_path: Path, scores_path: Path):
    records = load_manifest(corpus)["documents"]
    return records, _backend(corpus), _upcoming(upcoming_path, scores_path)


def _evaluate(args: argparse.Namespace) -> int:
    from .evaluate import evaluate
    from .flags import deployment_flags

    reason = _missing(args.corpus, args.events, args.upcoming)
    if reason:
        print(f"[eval] not run: {reason}")
        return NOT_RUN
    records, index, upcoming = _runtime(args.corpus, args.upcoming, args.scores)
    history = deployment_flags(pd.read_csv(args.events), mode="training")
    report = evaluate(records, index, history, upcoming)
    return _report(report, args.out)


def _report(report: dict, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    evidence = report["evidence"]
    print(f"[eval] evidence recall@5 {evidence['recall_at_5']} mrr {evidence['mrr']} "
          f"by type {evidence['recall_by_type']}")
    for route, stats in report["metadata_routes"].items():
        print(f"[eval] {route}: applied {stats['applied']}, "
              f"cited doc when applied {stats['returned_cited_doc_when_applied']}")
    print(f"[eval] upcoming verdicts {report['upcoming']['verdicts']}")
    for control in report["negative_controls"]:
        print(f"[eval] control {'pass' if control['passed'] else 'FAIL'}: {control['probe']} "
              f"-> {control['verdict']} ({control['generator_calls']} generator calls)")
    for gate, passed in report["gates"].items():
        print(f"[eval] gate {'pass' if passed else 'FAIL'}: {gate}")
    print(f"[eval] wrote {out}")
    return 0 if report["passed"] else 1


def _evaluate_generation(args: argparse.Namespace) -> int:
    from .evaluate import evaluate_generation, rejudge
    from .generate import judge_from_env

    if args.rejudge:
        judge = judge_from_env()
        reason = _missing(args.corpus, args.upcoming)
        if reason or judge is None or not args.rejudge.exists():
            print(f"[rejudge] not run: {reason or ('no judge in .env' if judge is None else f'{args.rejudge} not found')}")
            return NOT_RUN
        records, backend, upcoming = _runtime(args.corpus, args.upcoming, args.scores)
        saved = json.loads(args.rejudge.read_text(encoding="utf-8"))
        result = rejudge(saved, records, backend, upcoming, judge, repeat=args.repeat)
        out = args.rejudge.with_name(args.rejudge.stem + f"_rejudged_x{args.repeat}.json")
        out.write_text(json.dumps(result, indent=2, default=str) + "\n",
                       encoding="utf-8", newline="\n")
        for number, rates in enumerate(result["passes"], 1):
            print(f"[rejudge] pass {number}: {rates}")
        print(f"[rejudge] {result['explanations']} texts by {result['judge']}: claim precision "
              f"mean {result['precision_mean']}, spread {result['precision_spread']}; wrote {out}")
        return 0

    reason = _missing(args.corpus, args.upcoming)
    generator = _generator()
    if reason or generator is None:
        print(f"[eval-generation] not run: {reason or 'no generator configured in .env'}")
        return NOT_RUN
    judge = judge_from_env()
    records, backend, upcoming = _runtime(args.corpus, args.upcoming, args.scores)
    print(f"[eval-generation] writer {generator.name}, judge {judge.name if judge else 'none'}")
    report = evaluate_generation(records, backend, upcoming, generator, judge)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    for row in report["results"]:
        marks = "".join(
            ("." if row.get(check, True) else check[0].upper())
            for check in ("citations_ok", "numbers_ok", "flags_ok")
        ) if row["generated"] else "---"
        verdict = row.get("judge") or {}
        claims = (f"{verdict['supported']}/{verdict['supported'] + len(verdict['unsupported_claims'])}"
                  if verdict.get("precision") is not None else "-")
        print(f"[eval-generation] {row['probe']:<24} {row['verdict']:<22} checks {marks} "
              f"claims supported {claims}"
              f"{'' if row['refusal_correct'] else '  REFUSAL WRONG'}"
              f"{'  ERROR ' + row['error'] if row['error'] else ''}")
    print(f"[eval-generation] rates {report['rates']}")
    for gate, passed in report["gates"].items():
        print(f"[eval-generation] gate {'pass' if passed else 'FAIL'}: {gate}")
    print(f"[eval-generation] wrote {args.out}")
    return 0 if report["passed"] else 1


def _index(args: argparse.Namespace) -> int:
    """Build a new Search index, grade it, and promote it only if it passes.

    The alias readers use moves only after the eval's gates pass against the
    new index, so a bad build is left unpromoted for inspection and never
    served. Without the deployment CSVs there is no eval, and nothing is
    promoted unless that is asked for explicitly.
    """
    from .azure_search import AzureSearchBackend, SearchAdmin, foundry_embedder
    from .evaluate import evaluate
    from .flags import deployment_flags

    if not _search_configured():
        print("[index] not run: set FIRMAWARE_SEARCH_ENDPOINT, FIRMAWARE_FOUNDRY_ENDPOINT "
              "and FIRMAWARE_FOUNDRY_EMBEDDING_DEPLOYMENT in .env")
        return NOT_RUN
    search = os.environ["FIRMAWARE_SEARCH_ENDPOINT"]
    alias = os.getenv("FIRMAWARE_SEARCH_INDEX", "firmaware-corpus")
    embed = foundry_embedder(os.environ["FIRMAWARE_FOUNDRY_ENDPOINT"],
                             os.environ["FIRMAWARE_FOUNDRY_EMBEDDING_DEPLOYMENT"])
    admin = SearchAdmin(search, alias)
    chunks = chunk_corpus(args.corpus)
    name = admin.build(chunks, embed)
    print(f"[index] built {name}: {len(chunks)} chunks embedded and uploaded")

    if args.events.exists() and args.upcoming.exists():
        records = load_manifest(args.corpus)["documents"]
        candidate = AzureSearchBackend(search, name, embed)
        upcoming = _upcoming(args.upcoming, args.scores)
        history = deployment_flags(pd.read_csv(args.events), mode="training")
        report = evaluate(records, candidate, history, upcoming)
        if _report(report, Path("outputs") / f"rag_eval_{name}.json") != 0:
            print(f"[index] {name} failed a gate; left unpromoted, alias unchanged")
            return 1
    elif not args.promote_without_eval:
        print(f"[index] {name} built but not promoted: no eval data "
              f"({args.events}, {args.upcoming}); pass --promote-without-eval to force")
        return NOT_RUN

    previous = admin.promote(name)
    print(f"[index] alias {alias} -> {name} (was {previous or 'unset'}); "
          f"roll back by pointing it at {previous}" if previous else
          f"[index] alias {alias} -> {name}")
    removed = admin.prune(keep=args.keep)
    if removed:
        print(f"[index] removed older indexes: {', '.join(removed)}")
    return 0


def _explain(args: argparse.Namespace) -> int:
    from .explain import explain

    reason = _missing(args.corpus, args.upcoming)
    if reason:
        print(f"[explain] not run: {reason}")
        return NOT_RUN
    records, index, upcoming = _runtime(args.corpus, args.upcoming, args.scores)
    if args.deployment_id not in upcoming.index:
        print(f"[explain] {args.deployment_id} is not in {args.upcoming}")
        return 1
    generator = None if args.no_generate else _generator()
    result = explain(upcoming.loc[args.deployment_id], records, index, generator)
    print(result.context)
    print(f"[explain] verdict {result.assessment.verdict} {result.assessment.reasons or ''}")
    for route, hits in result.routes.items():
        found = ", ".join(f"{h.chunk['chunk_id']}({h.score:.3f})" for h in hits) or "-"
        print(f"[explain] {route:<12} {found}")
    if result.error:
        print(f"[explain] generation failed: {result.error}")
        return 1
    if result.text is None:
        print("[explain] generation skipped: no generator (set ANTHROPIC_API_KEY in .env)")
    else:
        print(result.text)
    if result.checks:
        print(f"[explain] grounding checks {result.checks}")
    return 0


def _generator():
    """Whatever .env configures (Foundry, else Claude); None skips generation."""
    from .generate import generator_from_env

    return generator_from_env()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _load_env()
    if args.command == "manifest":
        write_manifest(args.corpus, build_manifest(args.corpus, args.source))
        print(f"[manifest] wrote {args.corpus / 'manifest.json'}")
        return 0
    if args.command == "chunk":
        return _chunk(args.corpus, args.out)
    if args.command == "eval":
        return _evaluate(args)
    if args.command == "explain":
        return _explain(args)
    if args.command == "index":
        return _index(args)
    if args.command == "eval-generation":
        return _evaluate_generation(args)

    events = None
    if args.events.exists():
        events = pd.read_csv(
            args.events, usecols=["deployment_id", "vendor_name", "device_type"]
        )
    else:
        print(f"[lint] {args.events} not found; deployment ids not resolved")
    ids = None if events is None else set(events["deployment_id"])
    problems = lint(args.corpus, ids)
    for problem in problems:
        print(f"[lint] {problem}")
    if events is not None:
        _print_gold_report(args.corpus, events)
    print(f"[lint] {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
