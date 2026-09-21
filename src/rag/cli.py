"""``rag`` CLI — the one entry point for the knowledge pipeline.

    rag build --inbox data/ocr_backend/out [--enrich]
    rag status
    rag query "年假超过几天需要审批？" [-k 5] [--retrieve-only]
    rag eval tests/golden/rag_sample.jsonl

``build`` is offline and deterministic (LLM only with ``--enrich``);
``query`` is the online pipeline; ``eval`` is the measurement harness —
see ``docs/rag-subsystem-design.md`` for what each stage guarantees.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from utils.paths import data_dir as default_data_dir

from .answer import generate
from .contract import Outcome
from .engine import retrieve
from .evaluation import evaluate, load_cases
from .pipeline import build
from .store import RagStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rag", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir("rag"),
        help="project data directory (default: repo-root data/rag)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="offline: ingest -> chunk -> publish")
    p_build.add_argument(
        "--inbox",
        type=Path,
        default=default_data_dir("ocr_backend") / "out",
        help="directory of *.ocr.json bundles from `ocr-backend parse`",
    )
    p_build.add_argument(
        "--enrich",
        action="store_true",
        help="LLM-annotate chunks (titles/keywords/summaries) before indexing",
    )

    sub.add_parser("status", help="documents, snapshots, active version")

    p_query = sub.add_parser("query", help="online: retrieve (+ grounded answer)")
    p_query.add_argument("question")
    p_query.add_argument("-k", type=int, default=5)
    p_query.add_argument(
        "--retrieve-only",
        action="store_true",
        help="print the evidence pack and skip the LLM call",
    )

    p_eval = sub.add_parser("eval", help="run a golden set against retrieval")
    p_eval.add_argument("golden", type=Path)
    p_eval.add_argument("-k", type=int, default=5)
    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    report = build(args.inbox, args.data_dir, enrich=args.enrich)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 1


def _cmd_status(args: argparse.Namespace) -> int:
    store = RagStore(args.data_dir / "kb.db")
    print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    store.dispose()
    return 0


def _print_pack(pack) -> None:  # noqa: ANN001
    print(
        f"— {pack.strength.get('n_candidates', 0)} candidates "
        f"(best fts score: {pack.strength.get('best_score')})"
    )
    if pack.insufficient:
        print("! insufficient evidence:", "; ".join(pack.notes))
    for i, chunk in enumerate(pack.chunks, start=1):
        first_line = chunk.text.strip().splitlines()[0] if chunk.text else ""
        print(
            f"[{i}] {chunk.chunk_id} ({chunk.chunk_type}) "
            f"{chunk.section_path or '/'} :: {first_line[:70]}"
        )
    if pack.parents:
        print(f"  ({len(pack.parents)} parent section(s) attached as context)")


def _cmd_query(args: argparse.Namespace) -> int:
    store = RagStore(args.data_dir / "kb.db")
    pack = retrieve(store, args.question, k=args.k)
    _print_pack(pack)
    if args.retrieve_only:
        store.dispose()
        return 0
    answer = asyncio.run(generate(pack, args.question))
    print()
    print(f"outcome: {answer.outcome.value}")
    if answer.text:
        print(answer.text)
    for claim in answer.claims:
        refs = ", ".join(f"[{r}]" for r in claim.refs) or "(no citation)"
        print(f"  • {claim.text}  ← {refs}")
    if answer.clarification:
        print(f"clarification: {answer.clarification}")
    if answer.notes:
        print(f"notes: {answer.notes}")
    store.dispose()
    return 0 if answer.outcome in (Outcome.answered, Outcome.partial) else 2


def _cmd_eval(args: argparse.Namespace) -> int:
    store = RagStore(args.data_dir / "kb.db")
    cases = load_cases(args.golden)
    summary = evaluate(store, cases, k=args.k)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    store.dispose()
    return 0 if not summary["unresolved_case_ids"] else 1


_COMMANDS = {
    "build": _cmd_build,
    "status": _cmd_status,
    "query": _cmd_query,
    "eval": _cmd_eval,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inbox = getattr(args, "inbox", None)
    if inbox is not None and not inbox.is_dir():
        print(f"error: inbox directory not found: {inbox}", file=sys.stderr)
        return 1
    return _COMMANDS[args.command](args)
