"""rag — the knowledge pipeline: OCR output becomes retrievable, cited answers.

Vertical slice (M1, lexical-only) of the RAG subsystem designed in
``docs/rag/`` (overview: ``00-overview.md``): the pipeline shape and the
six data contracts are the architecture; every retrieval technology behind
a protocol seam (FTS5 now, grep/vector/rerank later) is a plug-in chosen by
``rag eval`` measurements, not by preference.

Typical use is the CLI (``rag build`` / ``rag query`` / ``rag eval``). For
library use: :func:`rag.pipeline.build`, :func:`rag.engine.retrieve`,
:func:`rag.answer.generate`.
"""

from .contract import (
    Answer,
    Candidate,
    CanonicalDoc,
    Chunk,
    EvidencePack,
    Outcome,
    PublishDecision,
)

__all__ = [
    "Answer",
    "CanonicalDoc",
    "Candidate",
    "Chunk",
    "EvidencePack",
    "Outcome",
    "PublishDecision",
]
