"""The offline vertical slice: acquire -> validate -> chunk -> publish.

Fully deterministic unless ``enrich=True``; idempotent per bundle (re-runs
upsert by doc_id and re-chunk under a NEW corpus_version — old snapshots
survive for rollback, matching CH03_02's versioned publish model).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ocr_backend.render import document_markdown

from .chunking import StructureAwareChunker
from .contract import CanonicalDoc, PublishDecision
from .enrich import LlmEnricher
from .ingest import OcrBundleAcquirer
from .store import RagStore

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


def build(
    inbox: Path,
    data_dir: Path,
    *,
    enrich: bool = False,
    store: RagStore | None = None,
) -> dict[str, Any]:
    store = store or RagStore(data_dir / "kb.db")
    acquirer = OcrBundleAcquirer()
    chunker = StructureAwareChunker()
    report: dict[str, Any] = {
        "inbox": str(inbox),
        "bundles": 0,
        "decisions": {},
        "errors": [],
        "chunked_docs": 0,
    }
    chunks = []
    indexed_docs: list[CanonicalDoc] = []

    for bundle in sorted(inbox.glob("*.ocr.json")):
        report["bundles"] += 1
        try:
            doc = acquirer.fetch(bundle)
        except Exception as exc:  # a broken bundle must not kill the build
            report["errors"].append(f"{bundle.name}: {exc}")
            continue
        store.upsert_document(doc)
        decision = doc.trust.publish_decision.value
        report["decisions"][decision] = report["decisions"].get(decision, 0) + 1
        if doc.trust.publish_decision not in INDEXABLE:
            continue
        chunks.extend(chunker.split(doc))
        indexed_docs.append(doc)

    if enrich and chunks:
        asyncio.run(LlmEnricher().enrich(chunks))

    version = store.next_version()
    store.stage_chunks(version, chunks)
    publish = store.publish(version, REPRESENTATIONS)
    _write_corpus_projection(data_dir / "corpus", indexed_docs)
    report.update(publish)
    report["chunked_docs"] = len(indexed_docs)
    return report
