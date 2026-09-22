"""``rag`` CLI — the one entry point for the knowledge pipeline.

    rag build --inbox data/ocr_backend/out          # ingest + publish
    rag ingest [--inbox DIR] [--limit N]            # stage new/changed docs
    rag publish                                     # diff transaction
    rag enrich [--limit N] [--prompt-ver V]         # LLM-annotate unenriched chunks
    rag retract <doc_id>                            # mark for removal at next publish
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

Declaration notes (post-migration from argparse, repo-wide click convention):
``--data-dir`` stays a *group* option, so it is spelled before the
subcommand exactly as the usage doc shows. Every token in
``docs/rag/03-usage.md`` survived the move unchanged, including the single
dash ``-k`` / ``-n``. A missing ``--inbox`` is rejected at parse time via
``click.ClickException`` — same stderr + exit 1 the old
``raise SystemExit("error: ...")`` produced, with click's ``Error: `` prefix.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from storage import SqliteCache
from utils.paths import data_dir as default_data_dir

from . import pipeline
from .answer import generate
from .contract import EvidencePack, Outcome
from .engine import retrieve
from .evaluation import evaluate, load_cases
from .store import RagStore
from .tracing import Tracer, load_trace, render_tree

TRACE_HELP = "record this run's spans to <data-dir>/traces/ as JSONL"


def _default_inbox() -> Path:
    return default_data_dir("ocr_backend") / "out"


def _check_inbox(ctx: Any, param: Any, value: Path) -> Path:
    """Fail before the first bundle is read, not halfway through a run."""
    del ctx, param
    if not value.is_dir():
        raise click.ClickException(f"inbox directory not found: {value}")
    return value


def inbox_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """``--inbox`` as build and ingest both spell it."""
    return click.option(
        "--inbox",
        type=Path,
        default=_default_inbox,
        callback=_check_inbox,
        help="directory of *.ocr.json bundles from `ocr-backend parse`",
    )(f)


def trace_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """``--trace`` on the four commands that run the pipeline."""
    return click.option("--trace", is_flag=True, help=TRACE_HELP)(f)


def _tracer(data_dir: Path, run: str, trace: bool) -> Tracer:
    if not trace:
        return Tracer.disabled()
    return Tracer(data_dir / "traces", run=run)


def _report_trace(path: Path | None) -> None:
    if path is not None:
        click.echo(f"\ntrace: {path}")


def _echo_json(payload: Any) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@click.group()
@click.option(
    "--data-dir",
    type=Path,
    default=default_data_dir("rag"),
    help="project data directory (default: repo-root data/rag)",
)
@click.pass_context
def cli(ctx: click.Context, data_dir: Path) -> None:
    """the knowledge pipeline over OCR output: build / query / eval / status"""
    ctx.obj = data_dir


@cli.command()
@inbox_option
@trace_option
@click.pass_obj
def build(data_dir: Path, inbox: Path, trace: bool) -> None:
    """offline: ingest -> chunk -> publish"""
    tracer = _tracer(data_dir, "build", trace)
    try:
        report = pipeline.build(inbox, data_dir, tracer=tracer)
    finally:
        _report_trace(tracer.close())
    _echo_json(report)
    raise SystemExit(0 if not report["errors"] else 1)


@cli.command()
@inbox_option
@click.option(
    "--limit", type=int, default=0, help="stop after N bundles (0 = no limit)"
)
@trace_option
@click.pass_obj
def ingest(data_dir: Path, inbox: Path, limit: int, trace: bool) -> None:
    """stage new/changed docs from inbox"""
    tracer = _tracer(data_dir, "ingest", trace)
    try:
        report = pipeline.ingest(inbox, data_dir, limit=limit, tracer=tracer)
    finally:
        _report_trace(tracer.close())
    _echo_json(report)
    raise SystemExit(0 if not report["errors"] else 1)


@cli.command()
@click.pass_obj
def publish(data_dir: Path) -> None:
    """run the diff transaction (staged -> active)"""
    _echo_json(pipeline.publish(data_dir))


@cli.command()
@click.option("--limit", type=int, default=0, help="stop after N chunks (0 = no limit)")
@click.option(
    "--prompt-ver",
    default=None,
    help="prompt version tag (default: versions.PROMPT_VER)",
)
@click.option(
    "--workers", type=int, default=4, help="concurrent LLM calls (default: 4)"
)
@click.pass_obj
def enrich(data_dir: Path, limit: int, prompt_ver: str | None, workers: int) -> None:
    """LLM-annotate unenriched chunks (independent background step)"""
    from .enrich import enrich_corpus
    from .versions import PROMPT_VER

    report = enrich_corpus(
        data_dir,
        limit=limit,
        prompt_ver=prompt_ver or PROMPT_VER,
        workers=workers,
    )
    _echo_json(report)
    raise SystemExit(1 if report["errors"] else 0)


@cli.command()
@click.argument("doc_id")
@click.pass_obj
def retract(data_dir: Path, doc_id: str) -> None:
    """mark a doc for removal at next publish"""
    store = RagStore(data_dir / "kb.db")
    store.retract(doc_id)
    click.echo(f"retracted: {doc_id}")
    store.dispose()


@cli.command()
@click.option(
    "--keep",
    type=int,
    default=3,
    help="retain the N most recent snapshots (default: 3)",
)
@click.pass_obj
def gc(data_dir: Path, keep: int) -> None:
    """prune old snapshots and orphaned chunks"""
    store = RagStore(data_dir / "kb.db")
    _echo_json(store.gc(keep=keep))
    store.dispose()


@cli.command()
@click.pass_obj
def status(data_dir: Path) -> None:
    """documents, snapshots, active version"""
    store = RagStore(data_dir / "kb.db")
    _echo_json(store.status())
    store.dispose()


def _print_pack(pack: EvidencePack) -> None:
    click.echo(
        f"— {pack.strength.get('n_candidates', 0)} candidates "
        f"(best fts score: {pack.strength.get('best_score')})"
    )
    if pack.insufficient:
        click.echo("! insufficient evidence: " + "; ".join(pack.notes))
    for i, chunk in enumerate(pack.chunks, start=1):
        first_line = chunk.text.strip().splitlines()[0] if chunk.text else ""
        click.echo(
            f"[{i}] {chunk.chunk_id} ({chunk.chunk_type}) "
            f"{chunk.section_path or '/'} :: {first_line[:70]}"
        )
    if pack.parents:
        click.echo(f"  ({len(pack.parents)} parent section(s) attached as context)")


@cli.command()
@click.argument("question")
@click.option("-k", type=int, default=5)
@click.option(
    "--retrieve-only",
    is_flag=True,
    help="print the evidence pack and skip the LLM call",
)
@trace_option
@click.pass_obj
def query(
    data_dir: Path, question: str, k: int, retrieve_only: bool, trace: bool
) -> None:
    """online: retrieve (+ grounded answer)"""
    tracer = _tracer(data_dir, "query", trace)
    store = RagStore(data_dir / "kb.db")
    cache = SqliteCache(data_dir / "cache.db")
    rc = 0
    try:
        with tracer.span("rag.query", question=question[:120]):
            pack = retrieve(store, question, k=k, tracer=tracer, cache=cache)
            _print_pack(pack)
            if not retrieve_only:
                with tracer.span(
                    "gen_ai.completion", **{"gen_ai.operation.name": "chat"}
                ) as csp:
                    answer = asyncio.run(generate(pack, question))
                    csp.set(outcome=answer.outcome.value, n_claims=len(answer.claims))
                click.echo()
                click.echo(f"outcome: {answer.outcome.value}")
                if answer.text:
                    click.echo(answer.text)
                for claim in answer.claims:
                    refs = ", ".join(f"[{r}]" for r in claim.refs) or "(no citation)"
                    click.echo(f"  • {claim.text}  ← {refs}")
                if answer.clarification:
                    click.echo(f"clarification: {answer.clarification}")
                if answer.notes:
                    click.echo(f"notes: {answer.notes}")
                rc = 0 if answer.outcome in (Outcome.answered, Outcome.partial) else 2
    finally:
        cache.close()
        store.dispose()
        _report_trace(tracer.close())
    raise SystemExit(rc)


@cli.command("eval")
@click.argument("golden", type=Path)
@click.option("-k", type=int, default=5)
@trace_option
@click.pass_obj
def eval_cmd(data_dir: Path, golden: Path, k: int, trace: bool) -> None:
    """run a golden set against retrieval"""
    tracer = _tracer(data_dir, "eval", trace)
    store = RagStore(data_dir / "kb.db")
    cases = load_cases(golden)
    try:
        summary = evaluate(store, cases, k=k, tracer=tracer)
    finally:
        store.dispose()
        _report_trace(tracer.close())
    _echo_json(summary)
    raise SystemExit(0 if not summary["unresolved_case_ids"] else 1)


@cli.command()
@click.argument("file", required=False, default=None)
@click.option("-n", type=int, default=10, help="how many to list")
@click.pass_obj
def traces(data_dir: Path, file: str | None, n: int) -> None:
    """list recorded runs; with FILE, render that trace as a tree"""
    traces_dir = data_dir / "traces"
    files = (
        sorted(traces_dir.glob("*.jsonl"), reverse=True) if traces_dir.is_dir() else []
    )
    if file:
        target = Path(file)
        if not target.is_file():
            matches = [f for f in files if file in f.name]
            if not matches:
                click.echo(f"error: no trace matching {file!r}", err=True)
                raise SystemExit(1)
            target = matches[0]
        click.echo(render_tree(load_trace(target)))
        return
    if not files:
        click.echo("no traces yet — run build/query/eval with --trace")
        return
    for f in files[:n]:
        spans = load_trace(f)
        root = spans[0] if spans else None
        total = root.duration_ms if root else "?"
        bad = any(s.status != "ok" for s in spans)
        click.echo(
            f"{f.name}  {len(spans)} span(s)  {total} ms{'  [error]' if bad else ''}"
        )


if __name__ == "__main__":
    cli()
