"""The offline pipeline: ingest -> publish.

``ingest`` scans the inbox, skips bundles already seen (by sha+fp), chunks
only new/changed docs, and stages them. ``publish`` runs the diff
transaction. ``build`` is a compose of the two — kept for tests and toy
corpora.

Incremental is the only path; a full rebuild is the degenerate case
(pipeline_fp change ⇒ every id changes ⇒ the same ingest loop produces a
corpus-wide diff). See ``docs/rag/05-incremental-design.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ocr_backend.render import document_markdown

from .chunking import StructureAwareChunker
from .contract import CanonicalDoc, PublishDecision
from .ingest import OcrBundleAcquirer
from .store import RagStore
from .tracing import NO_TRACER, TracedAcquirer, TracedChunker, Tracer
from .versions import pipeline_fp

INDEXABLE = frozenset({PublishDecision.pass_, PublishDecision.pass_with_warning})
REPRESENTATIONS = {"fts": "ready", "vector": "absent@bge-m3"}


def _write_corpus_projection(corpus_dir: Path, docs: list[CanonicalDoc]) -> None:
    """Markdown projections of published documents — free insurance for a
    future literal-grep CandidatePath (the corpus stays greppable without
    rag ever depending on the projection)."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        target = corpus_dir / f"{doc.doc_id}.md"
        target.write_text(document_markdown(doc.document), encoding="utf-8")


def ingest(
    inbox: Path,
    data_dir: Path,
    *,
    limit: int = 0,
    store: RagStore | None = None,
    tracer: Tracer | None = None,
) -> dict[str, Any]:
    """Scan inbox, skip (sha,fp)-seen, chunk new/changed, stage."""
    tracer = tracer or NO_TRACER
    store = store or RagStore(data_dir / "kb.db")
    acquirer = TracedAcquirer(OcrBundleAcquirer(), tracer)
    chunker = TracedChunker(StructureAwareChunker(), tracer)
    fp = pipeline_fp()
    report: dict[str, Any] = {
        "inbox": str(inbox),
        "bundles": 0,
        "decisions": {},
        "errors": [],
        "chunked_docs": 0,
        "skipped_docs": 0,
    }
    indexed_docs: list[CanonicalDoc] = []

    with tracer.span("rag.ingest", inbox=str(inbox), fp=fp) as isp:
        bundles = sorted(inbox.glob("*.ocr.json"))
        if limit > 0:
            bundles = bundles[:limit]
        for bundle in bundles:
            report["bundles"] += 1
            try:
                doc = acquirer.fetch(bundle)
            except Exception as exc:
                report["errors"].append(f"{bundle.name}: {exc}")
                continue
            store.upsert_document(doc)
            decision = doc.trust.publish_decision.value
            report["decisions"][decision] = report["decisions"].get(decision, 0) + 1
            if doc.trust.publish_decision not in INDEXABLE:
                continue
            # skip if already staged/live with same fp
            if _already_staged(store, doc, fp):
                report["skipped_docs"] += 1
                continue
            chunks = chunker.split(doc, fp=fp)
            store.stage_document(doc, chunks, fp)
            indexed_docs.append(doc)
            report["chunked_docs"] += 1
        isp.set(n_bundles=report["bundles"], n_chunked=report["chunked_docs"])

    # write corpus projections for newly indexed docs
    if indexed_docs:
        _write_corpus_projection(data_dir / "corpus", indexed_docs)

    return report


def _already_staged(store: RagStore, doc: CanonicalDoc, fp: str) -> bool:
    """Check if doc is already staged/live with the current fp."""
    from sqlalchemy import text

    with store.client.session() as db:
        row = db.execute(
            text("""SELECT ingest_state, chunked_with_fp FROM documents
                   WHERE doc_id = :doc_id"""),
            {"doc_id": doc.doc_id},
        ).first()
        if not row:
            return False
        return row[0] in ("staged", "live") and row[1] == fp


def publish(
    data_dir: Path,
    *,
    store: RagStore | None = None,
    tracer: Tracer | None = None,
) -> dict[str, Any]:
    """Run the diff transaction: copy base manifest ± staged/retracted, diff FTS."""
    tracer = tracer or NO_TRACER
    store = store or RagStore(data_dir / "kb.db")
    with tracer.span("rag.publish") as rsp:
        result = store.publish_diff(REPRESENTATIONS)
        rsp.set(**result)
    return result


def build(
    inbox: Path,
    data_dir: Path,
    *,
    store: RagStore | None = None,
    tracer: Tracer | None = None,
) -> dict[str, Any]:
    """Compose ingest + publish — kept for tests and toy corpora."""
    tracer = tracer or NO_TRACER
    store = store or RagStore(data_dir / "kb.db")
    with tracer.span("rag.build", inbox=str(inbox)) as rsp:
        ingest_report = ingest(inbox, data_dir, store=store, tracer=tracer)
        publish_report = publish(data_dir, store=store, tracer=tracer)
        rsp.set(
            corpus_version=publish_report["corpus_version"],
            n_chunks=publish_report["chunk_count"],
        )
    return {**ingest_report, **publish_report}
