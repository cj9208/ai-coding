"""LLM enrichment: synthetic retrieval aids, persisted as a side projection.

Enrich is an independent background step (``rag enrich``), not part of the
build pipeline. It scans live child chunks that lack an ``inferred`` row
for the current prompt version, calls the LLM, and writes the result
straight to the ``inferred`` table — the in-memory ``chunk.inferred`` dict
is never used. Each chunk is committed individually so a crash loses at
most one LLM call, and a re-run picks up where it left off.

Concurrency: uses TaskQueue with a dedicated "enrich" lane for parallel
LLM calls. Backpressure propagates naturally — when the lane queue is full,
submit() blocks; when LLM rate limiting kicks in, workers block, the queue
fills, and submit() blocks. Per-chunk checkpoint is preserved: each result
is written to the DB immediately after the LLM call succeeds.

Without ``rag enrich`` the pipeline stays fully deterministic; chunks are
searchable via the FTS fallback (section_path title, empty keywords /
summary). See ``docs/rag/04-scaling.md`` §5 and ``docs/rag/05-incremental-design.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field

from llm_client import LLMClient, get_client

from .store import RagStore
from .versions import PROMPT_VER, pipeline_fp

logger = logging.getLogger(__name__)

_MAX_INPUT_CHARS = 2000


class EnrichPartialError(RuntimeError):
    """Raised when one or more chunks fail enrichment — the error carries
    per-chunk failure details so the caller can report them."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} chunk(s) failed enrichment")


_SYSTEM = (
    "You annotate document chunks to make them findable. Produce a short "
    "title, search keywords, and a factual summary of the chunk ONLY — no "
    "outside knowledge. Keywords in the chunk's own language(s)."
)


class _Insight(BaseModel):
    title: str = ""
    keywords: list[str] = Field(default_factory=list)
    summary: str = ""


class LlmEnricher:
    """Implements :class:`rag.protocols.EnrichProvider`.

    When a *store* is provided, each chunk's result is persisted to the
    ``inferred`` table immediately (checkpoint-per-chunk). Without a store
    the result is written to ``chunk.inferred`` in memory only — kept for
    unit tests that exercise the LLM call in isolation.

    Uses a TaskQueue lane for concurrent LLM calls. Worker count is
    configurable; default 4 balances throughput against LLM rate limits.
    """

    name = "llm"

    def __init__(
        self,
        client: LLMClient | None = None,
        max_chunks: int = 500,
        store: RagStore | None = None,
        prompt_ver: str = PROMPT_VER,
        workers: int = 4,
    ):
        self.client = client
        self.max_chunks = max_chunks
        self.store = store
        self.prompt_ver = prompt_ver
        self.workers = workers

    async def enrich(self, chunks: list[Any]) -> None:
        client = self.client or get_client()

        todo: list[Any] = []
        for chunk in chunks[: self.max_chunks]:
            if chunk.is_parent:
                continue
            if self.store is not None and _already_enriched(
                self.store, chunk.chunk_id, self.prompt_ver
            ):
                continue
            todo.append(chunk)

        if not todo:
            return

        from task_queue import TaskQueue

        q = TaskQueue()
        q.lane("enrich", workers=self.workers)
        await q.start()

        try:

            async def _enrich_one(chunk: Any) -> tuple[str, _Insight]:
                raw = await client.chat_json(
                    f"Chunk text:\n{chunk.text[:_MAX_INPUT_CHARS]}",
                    system_prompt=_SYSTEM,
                    schema=_Insight,
                    temperature=0.0,
                )
                insight = (
                    raw if isinstance(raw, _Insight) else _Insight.model_validate(raw)
                )
                return chunk.chunk_id, insight

            futures = [
                await q.submit("enrich", _enrich_one, chunk, retries=3)
                for chunk in todo
            ]

            errors: list[str] = []

            for future in asyncio.as_completed(futures):
                try:
                    chunk_id, insight = await future
                    if self.store is not None:
                        self.store.write_inferred(
                            chunk_id,
                            self.prompt_ver,
                            title=insight.title,
                            keywords=" ".join(insight.keywords),
                            summary=insight.summary,
                        )
                    else:
                        for c in todo:
                            if c.chunk_id == chunk_id:
                                c.inferred = {
                                    "title": insight.title,
                                    "keywords": insight.keywords,
                                    "summary": insight.summary,
                                }
                                break
                except Exception as exc:
                    errors.append(f"{exc}")
                    logger.exception("enrich failed for chunk")

            if errors:
                raise EnrichPartialError(errors)
        finally:
            await q.stop()


def _already_enriched(store: RagStore, chunk_id: str, prompt_ver: str) -> bool:
    from sqlalchemy import text

    with store.client.session() as db:
        row = db.execute(
            text(
                "SELECT 1 FROM inferred" " WHERE chunk_id = :cid AND prompt_ver = :pv"
            ),
            {"cid": chunk_id, "pv": prompt_ver},
        ).first()
    return row is not None


def enrich_corpus(
    data_dir: Any,
    *,
    limit: int = 0,
    store: RagStore | None = None,
    prompt_ver: str = PROMPT_VER,
    workers: int = 4,
) -> dict[str, Any]:
    """Scan unenriched child chunks, LLM-annotate them, persist results.

    Reads chunk text from the DB, calls the LLM, writes each result to the
    ``inferred`` table, then refreshes the affected FTS rows. Returns a
    summary report.

    Uses a TaskQueue lane with ``workers`` concurrent LLM calls (default 4).
    """
    from pathlib import Path

    from .contract import Chunk

    store = store or RagStore(Path(data_dir) / "kb.db")
    fp = pipeline_fp()

    pending = store.unenriched_child_chunk_ids(prompt_ver, limit=limit)
    if not pending:
        return {
            "prompt_ver": prompt_ver,
            "pending": 0,
            "enriched": 0,
            "errors": [],
            "docs_refreshed": 0,
        }

    chunk_ids = [cid for cid, _ in pending]
    doc_ids = sorted({did for _, did in pending})

    from sqlalchemy import text

    chunks_by_id: dict[str, Chunk] = {}
    with store.client.session() as db:
        for cid in chunk_ids:
            row = db.execute(
                text(
                    "SELECT chunk_id, doc_id, chunk_type, is_parent,"
                    " parent_chunk_id, section_path, page_start, page_end,"
                    " block_keys, text, structured_payload, trust_level,"
                    " content_hash, pipeline_fp"
                    " FROM chunks WHERE chunk_id = :cid"
                ),
                {"cid": cid},
            ).first()
            if row:
                import json

                chunks_by_id[row[0]] = Chunk(
                    chunk_id=row[0],
                    doc_id=row[1],
                    chunk_type=row[2],
                    is_parent=bool(row[3]),
                    parent_chunk_id=row[4],
                    section_path=row[5],
                    text=row[9],
                    page_span=(row[6], row[7]),
                    block_keys=json.loads(row[8]),
                    trust_level=row[11],
                    content_hash=row[12],
                )

    ordered = [chunks_by_id[cid] for cid in chunk_ids if cid in chunks_by_id]

    enricher = LlmEnricher(
        max_chunks=len(ordered),
        store=store,
        prompt_ver=prompt_ver,
        workers=workers,
    )
    errors: list[str] = []
    try:
        asyncio.run(enricher.enrich(ordered))
    except EnrichPartialError as exc:
        errors.extend(exc.errors)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    docs_refreshed = 0
    if not errors:
        docs_refreshed = store.refresh_fts_for_docs(doc_ids, fp)

    return {
        "prompt_ver": prompt_ver,
        "pending": len(pending),
        "enriched": len(ordered),
        "errors": errors,
        "docs_refreshed": docs_refreshed,
    }
