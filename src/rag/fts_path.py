"""Fts5Path: the one CandidatePath of M1 — BM25 over the folded index.

Field weights follow field-aware indexing (CH03_02): title and section
path carry more signal than body, keywords/summary (inferred text) sit in
between. Graded relaxation mirrors file_manager: strict AND first; if
nothing matches, retry OR — recorded in outcome meta so the evidence pack
can say "this answer rested on a relaxed match".

Scores are reported as ``-bm25`` (higher = better) for display and
insufficiency notes; fusion ranks candidates by position, not raw score, so
the sign convention never matters across paths.
"""

from __future__ import annotations

from sqlalchemy import text

from storage import match_expr

from .contract import Candidate
from .protocols import SearchOutcome, ShapedQuery

# bm25() weights, positional over (title, body, section_path, keywords,
# summary). Inlined as literals: SQLite requires constant arguments to
# bm25(); these are ours, never user input.
_WEIGHTS = (5.0, 1.0, 2.0, 1.5, 1.0)

_SQL = (
    f"SELECT c.chunk_id AS chunk_id, "  # nosec B608 — only _WEIGHTS literals
    f'bm25(chunks_fts, {", ".join(str(w) for w in _WEIGHTS)}) AS rank_score '
    "FROM chunks_fts "
    "JOIN chunks c ON c.rowid = chunks_fts.rowid "
    "JOIN snapshot_docs sd ON sd.doc_id = c.doc_id AND sd.pipeline_fp = c.pipeline_fp "
    "WHERE chunks_fts MATCH :m AND sd.corpus_version = :v AND c.is_parent = 0 "
    "ORDER BY rank_score "
    "LIMIT :k"
)


class Fts5Path:
    """Implements :class:`rag.protocols.CandidatePath` for the lexical
    projection of the active snapshot."""

    name = "fts"

    def __init__(self, client, version: int):  # noqa: ANN001 - SqliteClient
        self.client = client
        self.version = version

    def _run(self, shaped: ShapedQuery, k: int, joiner: str) -> list[Candidate]:
        match = match_expr(shaped.tokens, joiner=joiner)
        if not match:
            return []
        params: dict = {"m": match, "v": self.version, "k": k}
        with self.client.session() as db:
            rows = db.execute(text(_SQL), params).fetchall()
        return [
            Candidate(
                chunk_id=r[0],
                # bm25() is lower-is-better; flip once at the edge so every
                # consumer sees "higher = better" without knowing the engine
                scores={self.name: -float(r[1])},
                ranks={self.name: rank},
            )
            for rank, r in enumerate(rows, start=1)
        ]

    def search(self, shaped: ShapedQuery, k: int) -> SearchOutcome:
        candidates = self._run(shaped, k, " AND ")
        relaxed = False
        if not candidates:
            candidates = self._run(shaped, k, " OR ")
            relaxed = bool(candidates)
        return SearchOutcome(
            candidates=candidates,
            meta={"path": self.name, "relaxed": relaxed},
        )
