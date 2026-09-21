"""``rag`` CLI — the one entry point for the knowledge pipeline.

    rag build --inbox data/ocr_backend/out          # ingest + publish
    rag ingest [--inbox DIR] [--limit N]            # stage new/changed docs
    rag publish                                     # diff transaction
    rag enrich [--limit N] [--prompt-ver V]         # LLM-annotate unenriched chunks
    rag retract <doc_id>                            # mark for removal
    rag gc [--keep N]                               # prune old snapshots
    rag status
    rag query "年假超过几天需要审批？" [-k 5] [--retrieve-only]
    rag eval tests/golden/rag_sample.jsonl
    rag traces [-n 10] [FILE]

Any of build/ingest/query/eval accepts ``--trace``: the run is recorded as
one JSONL span file under ``<data-dir>/traces/`` (see ``rag.tracing``), and
``traces`` lists / renders those records.

``build`` is offline and deterministic; ``enrich`` is the only LLM touch
in the offline pipeline and runs as an independent background step;
``query`` is the online pipeline; ``eval`` is the measurement harness —
see ``docs/rag/02-implementation.md`` for what each stage guarantees and
``docs/rag/03-usage.md`` for the full CLI reference.
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
from .pipeline import build, ingest, publish
from .store import RagStore
from .tracing import Tracer, load_trace, render_tree


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

    p_ingest = sub.add_parser("ingest", help="stage new/changed docs from inbox")
    p_ingest.add_argument(
        "--inbox",
        type=Path,
        default=default_data_dir("ocr_backend") / "out",
        help="directory of *.ocr.json bundles from `ocr-backend parse`",
    )
    p_ingest.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N bundles (0 = no limit)",
    )

    sub.add_parser("publish", help="run the diff transaction (staged -> active)")

    p_enrich = sub.add_parser(
        "enrich", help="LLM-annotate unenriched chunks (independent background step)"
    )
    p_enrich.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N chunks (0 = no limit)",
    )
    p_enrich.add_argument(
        "--prompt-ver",
        default=None,
        help="prompt version tag (default: versions.PROMPT_VER)",
    )
    p_enrich.add_argument(
        "--workers",
        type=int,
        default=4,
        help="concurrent LLM calls (default: 4)",
    )

    p_retract = sub.add_parser("retract", help="mark a doc for removal at next publish")
    p_retract.add_argument("doc_id", help="document id to retract")

    p_gc = sub.add_parser("gc", help="prune old snapshots and orphaned chunks")
    p_gc.add_argument(
        "--keep",
        type=int,
        default=3,
        help="retain the N most recent snapshots (default: 3)",
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

    p_traces = sub.add_parser(
        "traces", help="list recorded runs; with FILE, render that trace as a tree"
    )
    p_traces.add_argument(
        "file", nargs="?", help="trace JSONL (path or substring of a listed name)"
    )
    p_traces.add_argument("-n", type=int, default=10, help="how many to list")

    for sp in (p_build, p_ingest, p_query, p_eval):
        sp.add_argument(
            "--trace",
            action="store_true",
            help="record this run's spans to <data-dir>/traces/ as JSONL",
        )
    return parser


def _tracer(args: argparse.Namespace) -> Tracer:
    if not getattr(args, "trace", False):
        return Tracer.disabled()
    return Tracer(args.data_dir / "traces", run=args.command)


def _report_trace(path: Path | None) -> None:
    if path is not None:
        print(f"\ntrace: {path}")


def _cmd_build(args: argparse.Namespace) -> int:
    tracer = _tracer(args)
    try:
        report = build(args.inbox, args.data_dir, tracer=tracer)
    finally:
        _report_trace(tracer.close())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 1


def _cmd_ingest(args: argparse.Namespace) -> int:
    tracer = _tracer(args)
    try:
        report = ingest(
            args.inbox,
            args.data_dir,
            limit=args.limit,
            tracer=tracer,
        )
    finally:
        _report_trace(tracer.close())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["errors"] else 1


def _cmd_publish(args: argparse.Namespace) -> int:
    report = publish(args.data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    from .enrich import enrich_corpus
    from .versions import PROMPT_VER

    prompt_ver = args.prompt_ver or PROMPT_VER
    report = enrich_corpus(
        args.data_dir,
        limit=args.limit,
        prompt_ver=prompt_ver,
        workers=args.workers,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


def _cmd_retract(args: argparse.Namespace) -> int:
    store = RagStore(args.data_dir / "kb.db")
    store.retract(args.doc_id)
    print(f"retracted: {args.doc_id}")
    store.dispose()
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    store = RagStore(args.data_dir / "kb.db")
    result = store.gc(keep=args.keep)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    store.dispose()
    return 0


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
    tracer = _tracer(args)
    store = RagStore(args.data_dir / "kb.db")
    rc = 0
    try:
        with tracer.span("rag.query", question=args.question[:120]):
            pack = retrieve(store, args.question, k=args.k, tracer=tracer)
            _print_pack(pack)
            if not args.retrieve_only:
                with tracer.span(
                    "gen_ai.completion", **{"gen_ai.operation.name": "chat"}
                ) as csp:
                    answer = asyncio.run(generate(pack, args.question))
                    csp.set(outcome=answer.outcome.value, n_claims=len(answer.claims))
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
                rc = 0 if answer.outcome in (Outcome.answered, Outcome.partial) else 2
    finally:
        store.dispose()
        _report_trace(tracer.close())
    return rc


def _cmd_eval(args: argparse.Namespace) -> int:
    tracer = _tracer(args)
    store = RagStore(args.data_dir / "kb.db")
    cases = load_cases(args.golden)
    try:
        summary = evaluate(store, cases, k=args.k, tracer=tracer)
    finally:
        store.dispose()
        _report_trace(tracer.close())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["unresolved_case_ids"] else 1


def _cmd_traces(args: argparse.Namespace) -> int:
    traces_dir = args.data_dir / "traces"
    files = (
        sorted(traces_dir.glob("*.jsonl"), reverse=True) if traces_dir.is_dir() else []
    )
    if args.file:
        target = Path(args.file)
        if not target.is_file():
            matches = [f for f in files if args.file in f.name]
            if not matches:
                print(f"error: no trace matching {args.file!r}", file=sys.stderr)
                return 1
            target = matches[0]
        print(render_tree(load_trace(target)))
        return 0
    if not files:
        print("no traces yet — run build/query/eval with --trace")
        return 0
    for f in files[: args.n]:
        spans = load_trace(f)
        root = spans[0] if spans else None
        total = root.duration_ms if root else "?"
        bad = any(s.status != "ok" for s in spans)
        print(f"{f.name}  {len(spans)} span(s)  {total} ms{'  [error]' if bad else ''}")
    return 0


_COMMANDS = {
    "build": _cmd_build,
    "ingest": _cmd_ingest,
    "publish": _cmd_publish,
    "enrich": _cmd_enrich,
    "retract": _cmd_retract,
    "gc": _cmd_gc,
    "status": _cmd_status,
    "query": _cmd_query,
    "eval": _cmd_eval,
    "traces": _cmd_traces,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inbox = getattr(args, "inbox", None)
    if inbox is not None and not inbox.is_dir():
        print(f"error: inbox directory not found: {inbox}", file=sys.stderr)
        return 1
    return _COMMANDS[args.command](args)
