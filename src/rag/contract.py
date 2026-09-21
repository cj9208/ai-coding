"""The six data contracts that form the rag pipeline's spine.

Why contracts and not stages are the frozen part: the corpus is not chosen
yet, so every *technology* behind a stage (parsing source, lexical engine,
vector store, generator) is a swappable plug-in — but the objects crossing
the seams must be stable, or adding a plug-in means a rewrite. See
``docs/rag/01-design-rationale.md``.

Design rules (same as ``ocr_backend.contract``): JSON-native pydantic,
unknown fields ignored on load, data settles into plain types.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ocr_backend.contract import OcrDocument
from storage import sha256_hex

SCHEMA_VERSION = "1.0"


def block_key(page_index: int, block_id: int) -> str:
    """Stable address of one OCR block inside one document."""
    return f"{page_index}:{block_id}"


class PublishDecision(StrEnum):
    """Outcome of the ingestion validation gate (CH03_01 publish policy).

    Only ``pass`` and ``pass_with_warning`` documents may be chunked and
    indexed; quarantined documents stay in the DB, inspectable.
    """

    pass_ = "pass"  # nosec B105 — gate decision label, not a credential
    pass_with_warning = "pass_with_warning"  # nosec B105
    quarantine = "quarantine"
    fail = "fail"


class SourceInfo(BaseModel):
    """Lineage: what produced this document and whether a human vetted it."""

    kind: str
    path: str
    sha256: str
    extractor: str
    page_count: int
    reviewed: bool = False
    """True when an ocr-review sidecar was folded in — the human gate."""
    created_at: str


class TrustInfo(BaseModel):
    """Validation artifacts; kept beside content, never merged into it."""

    publish_decision: PublishDecision = PublishDecision.pass_
    risk_flags: list[str] = Field(default_factory=list)
    quality: dict[str, float] = Field(default_factory=dict)


class CanonicalDoc(BaseModel):
    """An OcrDocument plus everything rag adds *around* the contract.

    Deliberately does not mutate ``document``: annotations live in
    ``section_paths`` and ``trust``, mirroring ocr-review's sidecar
    philosophy — the machine JSON is never rewritten.
    """

    schema_version: str = SCHEMA_VERSION
    doc_id: str
    source: SourceInfo
    document: OcrDocument
    section_paths: dict[str, str] = Field(default_factory=dict)
    """``block_key -> "1 Intro > 1.2 Scope"``, rebuilt from title blocks."""
    trust: TrustInfo = Field(default_factory=TrustInfo)

    @staticmethod
    def make_doc_id(source_sha256: str) -> str:
        return "d" + sha256_hex(source_sha256, length=12)


class ChunkType(StrEnum):
    """Retrieval-unit kinds; drives chunking rules and assembly hints."""

    prose = "prose"
    table = "table"
    list = "list"
    formula = "formula"
    section = "section"  # parent chunk
    other = "other"


class Chunk(BaseModel):
    """The single truth of the retrieval world: everything indexed is a
    rebuildable projection over chunks, and a chunk traces back to blocks."""

    chunk_id: str
    """Content-addressed (see ``chunk_id_for``): stable across rebuilds
    while its text does not change — the key space future representations
    (embeddings) will be joined on."""

    doc_id: str
    chunk_type: ChunkType
    is_parent: bool
    parent_chunk_id: str | None
    section_path: str
    text: str
    structured_payload: dict[str, Any] | None = None
    page_span: tuple[int, int]
    block_keys: list[str] = Field(default_factory=list)
    content_hash: str
    trust_level: str = PublishDecision.pass_.value
    inferred: dict[str, Any] = Field(default_factory=dict)
    """LLM-derived retrieval aids (title/keywords/summary), stored apart
    from source text — CH03_02's authoritative-vs-inferred separation."""

    @staticmethod
    def chunk_id_for(
        doc_id: str, section_path: str, block_keys: list[str], *, fp: str = ""
    ) -> str:
        return sha256_hex("|".join([fp, doc_id, section_path, *block_keys]), length=12)

    @staticmethod
    def content_hash_for(text: str) -> str:
        return sha256_hex(text, length=16)


class Candidate(BaseModel):
    """One retrieval path's opinion about one chunk.

    The pivot of the plug-in design: paths never speak of engines, only of
    chunk ids and their own scores, so fusion/assembly/answering survive the
    addition of any new path.
    """

    chunk_id: str
    scores: dict[str, float] = Field(default_factory=dict)
    ranks: dict[str, int] = Field(default_factory=dict)


class EvidencePack(BaseModel):
    """Assembled, citation-ready context — retrieval's final word.

    ``chunks`` order defines citation anchors: chunk at index i is [i+1].
    ``parents`` are section-level context brought in by expansion: they
    inform the generator but are NOT citable (they duplicate child text).
    """

    query: str
    chunks: list[Chunk] = Field(default_factory=list)
    parents: list[Chunk] = Field(default_factory=list)
    strength: dict[str, Any] = Field(default_factory=dict)
    insufficient: bool = False
    notes: list[str] = Field(default_factory=list)


class Outcome(StrEnum):
    """The five explicit answers of CH03_04 — no silent sixth."""

    answered = "answered"
    partial = "partial"
    clarify = "clarify"
    insufficient = "insufficient"
    escalated = "escalated"


class Claim(BaseModel):
    text: str
    refs: list[int] = Field(default_factory=list)
    """1-based citation anchors into ``EvidencePack.chunks``."""


class Answer(BaseModel):
    outcome: Outcome
    text: str = ""
    claims: list[Claim] = Field(default_factory=list)
    clarification: str = ""
    notes: str = ""
