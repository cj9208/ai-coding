"""The five protocol seams — the plug-in surface of the pipeline.

The corpus is not chosen yet, so what we freeze is *where variation is
allowed*: acquisition source, chunking strategy, query shaping, retrieval
path, enrichment provider. Each protocol currently has exactly one
implementation; no second adapter is pre-written, because an interface
validated only by one implementation is a guess, and internal code carries
no compatibility burden.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .contract import Candidate, CanonicalDoc, Chunk


class ShapedQuery(BaseModel):
    """Output of query shaping: tokens are retrieval-engine-agnostic."""

    raw: str
    tokens: list[str]
    relaxed: bool = False
    """True when the strict AND form produced nothing and we fell back to OR."""


class Acquirer(Protocol):
    """Turn one on-disk bundle into a validated CanonicalDoc."""

    def fetch(self, bundle: Path) -> CanonicalDoc: ...


class ChunkStrategy(Protocol):
    """Split one canonical document into retrieval units."""

    def split(self, doc: CanonicalDoc, *, fp: str = "") -> list[Chunk]: ...


class QueryShaper(Protocol):
    def shape(self, query: str) -> ShapedQuery: ...


class SearchOutcome(BaseModel):
    """A path's candidates plus engine-level facts the assembler may report
    (e.g. ``{"relaxed": true}`` when strict AND matched nothing)."""

    candidates: list[Candidate] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class CandidatePath(Protocol):
    """One retrieval access path. ``name`` keys Candidate.scores/ranks, so
    fusion and observability never need to know what engine produced a
    candidate."""

    name: str

    def search(self, shaped: ShapedQuery, k: int) -> SearchOutcome: ...


class EnrichProvider(Protocol):
    """Fill ``chunk.inferred`` in place; must not touch source text."""

    async def enrich(self, chunks: list[Chunk]) -> None: ...
