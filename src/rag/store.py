"""SQLite persistence for rag: five tables, one publish transaction.

Schema knowledge (why these five):
- ``chunks`` is the only truth; ``chunks_fts`` and ``embeddings`` are
  rebuildable projections keyed on it (FTS by rowid, embeddings by
  content-addressed chunk_id). Adding a representation later = a backfill
  job, never a schema surgery.
- ``snapshots.representations`` records which projections are live for a
  version — the query engine runs paths only for registered ones, so a
  vector index that exists but was never backfilled is invisible, not half-
  effective.
- ``documents.canonical_json`` keeps the full annotation payload for
  traceability (playground scale; the bundle on disk stays the real source).

Publish is ONE transaction (stage rows already exist under the new
``corpus_version``): wipe FTS, re-index the new version's chunks, record the
snapshot, flip the active pointer. SQLite's atomic commit is what buys
CH03_02's "retrieval never sees a half-built index" — no external staging
system needed at this scale.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from storage import FtsTable, SqliteClient

from .contract import CanonicalDoc, Chunk

_FTS_COLUMNS = ("title", "body", "section_path", "keywords", "summary")

_DDL = (
    """
CREATE TABLE IF NOT EXISTS documents (
    doc_id           TEXT PRIMARY KEY,
    source_sha256    TEXT NOT NULL,
    source_path      TEXT NOT NULL,
    extractor        TEXT NOT NULL,
    reviewed         INTEGER NOT NULL DEFAULT 0,
    publish_decision TEXT NOT NULL,
    risk_flags       TEXT NOT NULL DEFAULT '[]',
    quality          TEXT NOT NULL DEFAULT '{}',
    page_count       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT '',
    canonical_json   TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id           TEXT NOT NULL,
    doc_id             TEXT NOT NULL REFERENCES documents(doc_id),
    corpus_version     INTEGER NOT NULL,
    chunk_type         TEXT NOT NULL,
    is_parent          INTEGER NOT NULL,
    parent_chunk_id    TEXT,
    section_path       TEXT NOT NULL DEFAULT '',
    page_start         INTEGER NOT NULL,
    page_end           INTEGER NOT NULL,
    block_keys         TEXT NOT NULL,
    text               TEXT NOT NULL,
    structured_payload TEXT,
    trust_level        TEXT NOT NULL DEFAULT 'pass',
    inferred           TEXT NOT NULL DEFAULT '{}',
    content_hash       TEXT NOT NULL,
    -- content-addressed ids repeat across versions by design (same text,
    -- same id); versioned history is exactly what rollback needs
    PRIMARY KEY (chunk_id, corpus_version)
)
""",
    "CREATE INDEX IF NOT EXISTS chunks_version ON chunks(corpus_version)",
    """
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id    TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    embedded_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, model_id)
)
""",
    """
CREATE TABLE IF NOT EXISTS snapshots (
    corpus_version  INTEGER PRIMARY KEY,
    published_at    TEXT NOT NULL,
    doc_count       INTEGER NOT NULL,
    chunk_count     INTEGER NOT NULL,
    representations TEXT NOT NULL
)
""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def db_path_for(data_dir: Path) -> Path:
    return data_dir / "kb.db"


def row_to_chunk(row: Any) -> Chunk:
    return Chunk(
        chunk_id=row.chunk_id,
        doc_id=row.doc_id,
        chunk_type=row.chunk_type,
        is_parent=bool(row.is_parent),
        parent_chunk_id=row.parent_chunk_id,
        section_path=row.section_path,
        text=row.text,
        structured_payload=(
            json.loads(row.structured_payload) if row.structured_payload else None
        ),
        page_span=(row.page_start, row.page_end),
        block_keys=json.loads(row.block_keys),
        content_hash=row.content_hash,
        trust_level=row.trust_level,
        inferred=json.loads(row.inferred),
    )


class RagStore:
    def __init__(self, db: Path):
        self.client = SqliteClient(db)
        self.fts = FtsTable("chunks_fts", _FTS_COLUMNS)
        self._init()

    def _init(self) -> None:
        with self.client.session() as db:
            for stmt in _DDL:
                db.execute(text(stmt))
            self.fts.create(db)
            db.commit()

    # -- documents ---------------------------------------------------------

    def upsert_document(self, doc: CanonicalDoc) -> None:
        with self.client.session() as db:
            db.execute(
                text("""INSERT INTO documents (doc_id, source_sha256, source_path,
                           extractor, reviewed, publish_decision, risk_flags,
                           quality, page_count, created_at, canonical_json)
                       VALUES (:doc_id, :sha, :path, :extractor, :reviewed,
                               :decision, :flags, :quality, :pages, :created, :json)
                       ON CONFLICT(doc_id) DO UPDATE SET
                           source_sha256=excluded.source_sha256,
                           source_path=excluded.source_path,
                           extractor=excluded.extractor,
                           reviewed=excluded.reviewed,
                           publish_decision=excluded.publish_decision,
                           risk_flags=excluded.risk_flags,
                           quality=excluded.quality,
                           page_count=excluded.page_count,
                           created_at=excluded.created_at,
                           canonical_json=excluded.canonical_json"""),
                {
                    "doc_id": doc.doc_id,
                    "sha": doc.source.sha256,
                    "path": doc.source.path,
                    "extractor": doc.source.extractor,
                    "reviewed": int(doc.source.reviewed),
                    "decision": doc.trust.publish_decision.value,
                    "flags": json.dumps(doc.trust.risk_flags),
                    "quality": json.dumps(doc.trust.quality),
                    "pages": doc.source.page_count,
                    "created": doc.source.created_at,
                    "json": doc.model_dump_json(),
                },
            )
            db.commit()

    # -- staging -------------------------------------------------------------

    def next_version(self) -> int:
        with self.client.session() as db:
            row = db.execute(
                text("SELECT COALESCE(MAX(corpus_version), 0) FROM chunks")
            ).one()
            active = self._active_in(db)
            return max(int(row[0]), active or 0) + 1

    def stage_chunks(self, version: int, chunks: list[Chunk]) -> None:
        with self.client.session() as db:
            db.execute(
                text("DELETE FROM chunks WHERE corpus_version = :v"), {"v": version}
            )
            for c in chunks:
                db.execute(
                    text("""INSERT INTO chunks (chunk_id, doc_id, corpus_version,
                               chunk_type, is_parent, parent_chunk_id, section_path,
                               page_start, page_end, block_keys, text,
                               structured_payload, trust_level, inferred,
                               content_hash)
                           VALUES (:chunk_id, :doc_id, :v, :chunk_type, :is_parent,
                                   :parent_chunk_id, :section_path, :page_start,
                                   :page_end, :block_keys, :text, :payload,
                                   :trust_level, :inferred, :content_hash)"""),
                    {
                        "chunk_id": c.chunk_id,
                        "doc_id": c.doc_id,
                        "v": version,
                        "chunk_type": str(c.chunk_type),
                        "is_parent": int(c.is_parent),
                        "parent_chunk_id": c.parent_chunk_id,
                        "section_path": c.section_path,
                        "page_start": c.page_span[0],
                        "page_end": c.page_span[1],
                        "block_keys": json.dumps(c.block_keys),
                        "text": c.text,
                        "payload": (
                            json.dumps(c.structured_payload)
                            if c.structured_payload
                            else None
                        ),
                        "trust_level": c.trust_level,
                        "inferred": json.dumps(c.inferred),
                        "content_hash": c.content_hash,
                    },
                )
            db.commit()

    # -- publish -------------------------------------------------------------

    def publish(self, version: int, representations: dict[str, str]) -> dict[str, Any]:
        """Atomic swap: re-index FTS for this version and flip the pointer."""
        with self.client.session() as db:
            db.execute(text("DELETE FROM chunks_fts"), {})
            rows = db.execute(
                text(
                    "SELECT rowid, chunk_id, doc_id, section_path, text, inferred "
                    "FROM chunks WHERE corpus_version = :v"
                ),
                {"v": version},
            ).fetchall()
            for r in rows:
                inferred = json.loads(r.inferred)
                self.fts.upsert(
                    db,
                    r.rowid,
                    {
                        "title": str(inferred.get("title", ""))
                        or r.section_path.split(" > ")[-1],
                        "body": r.text,
                        "section_path": r.section_path,
                        "keywords": " ".join(inferred.get("keywords", [])),
                        "summary": str(inferred.get("summary", "")),
                    },
                )
            docs = db.execute(
                text(
                    "SELECT COUNT(DISTINCT doc_id) FROM chunks "
                    "WHERE corpus_version = :v"
                ),
                {"v": version},
            ).one()
            published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.execute(
                text("""INSERT INTO snapshots (corpus_version, published_at,
                           doc_count, chunk_count, representations)
                       VALUES (:v, :at, :docs, :chunks, :repr)
                       ON CONFLICT(corpus_version) DO UPDATE SET
                           published_at=excluded.published_at,
                           doc_count=excluded.doc_count,
                           chunk_count=excluded.chunk_count,
                           representations=excluded.representations"""),
                {
                    "v": version,
                    "at": published_at,
                    "docs": int(docs[0]),
                    "chunks": len(rows),
                    "repr": json.dumps(representations),
                },
            )
            db.execute(
                text("""INSERT INTO meta (key, value) VALUES ('active_version', :v)
                       ON CONFLICT(key) DO UPDATE SET value = :v"""),
                {"v": str(version)},
            )
            db.commit()
        return {
            "corpus_version": version,
            "doc_count": int(docs[0]),
            "chunk_count": len(rows),
        }

    def _active_in(self, db) -> int | None:  # noqa: ANN001
        row = db.execute(
            text("SELECT value FROM meta WHERE key = 'active_version'")
        ).first()
        return int(row[0]) if row else None

    def active_version(self) -> int | None:
        with self.client.session() as db:
            return self._active_in(db)

    def snapshot(self, version: int) -> dict[str, Any] | None:
        with self.client.session() as db:
            row = (
                db.execute(
                    text("SELECT * FROM snapshots WHERE corpus_version = :v"),
                    {"v": version},
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            out = dict(row)
            out["representations"] = json.loads(out["representations"])
            return out

    # -- reads ----------------------------------------------------------------

    def chunks_in(self, version: int) -> list[Chunk]:
        with self.client.session() as db:
            rows = (
                db.execute(
                    text("SELECT * FROM chunks WHERE corpus_version = :v"),
                    {"v": version},
                )
                .mappings()
                .all()
            )
            return [row_to_chunk(r) for r in rows]

    def chunk_ids_matching(self, version: int, substrings: list[str]) -> set[str]:
        """Evaluate golden cases without pinning ids: a chunk matches when
        its text contains every substring (rebuild-proof via content
        addressing, readable in the JSONL)."""
        if not substrings:
            return set()
        clauses = " AND ".join(f"text LIKE :s{i}" for i in range(len(substrings)))
        params: dict[str, Any] = {f"s{i}": f"%{s}%" for i, s in enumerate(substrings)}
        params["v"] = version
        with self.client.session() as db:
            rows = db.execute(
                text(
                    f"SELECT chunk_id FROM chunks WHERE corpus_version = :v "  # nosec B608
                    f"AND is_parent = 0 AND {clauses}"
                ),
                params,
            ).fetchall()
            return {r[0] for r in rows}

    def status(self) -> dict[str, Any]:
        with self.client.session() as db:
            decisions = db.execute(
                text(
                    "SELECT publish_decision, COUNT(*) FROM documents "
                    "GROUP BY publish_decision"
                )
            ).fetchall()
            versions = db.execute(
                text(
                    "SELECT corpus_version, COUNT(*) FROM chunks GROUP BY corpus_version"
                )
            ).fetchall()
            snaps = db.execute(
                text(
                    "SELECT corpus_version, published_at, doc_count, chunk_count "
                    "FROM snapshots ORDER BY corpus_version"
                )
            ).fetchall()
            return {
                "documents_by_decision": {r[0]: r[1] for r in decisions},
                "staged_or_live": {int(r[0]): r[1] for r in versions},
                "active_version": self._active_in(db),
                "snapshots": [list(r) for r in snaps],
            }

    def dispose(self) -> None:
        self.client.dispose()
