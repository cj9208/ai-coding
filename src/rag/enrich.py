"""LLM enrichment: synthetic retrieval aids, kept in ``chunk.inferred``.

Opt-in at build time (``--enrich``) because it is the only LLM touch in the
offline pipeline; without it the pipeline stays fully deterministic.
Writes never merge into source text — the inferred dict is a separate
column, indexed separately, and the summary/keywords exist only to make
weak local chunks retrievable (CH03_02 synthetic retrieval aids).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from llm_client import LLMClient, get_client

from .contract import Chunk

_MAX_INPUT_CHARS = 2000

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
    """Implements :class:`rag.protocols.EnrichProvider`."""

    name = "llm"

    def __init__(self, client: LLMClient | None = None, max_chunks: int = 500):
        self.client = client
        self.max_chunks = max_chunks

    async def enrich(self, chunks: list[Chunk]) -> None:
        client = self.client or get_client()
        for chunk in chunks[: self.max_chunks]:
            if chunk.is_parent or chunk.inferred:
                continue
            raw = await client.chat_json(
                f"Chunk text:\n{chunk.text[:_MAX_INPUT_CHARS]}",
                system_prompt=_SYSTEM,
                schema=_Insight,
                temperature=0.0,
            )
            insight = raw if isinstance(raw, _Insight) else _Insight.model_validate(raw)
            chunk.inferred = {
                "title": insight.title,
                "keywords": insight.keywords,
                "summary": insight.summary,
            }
