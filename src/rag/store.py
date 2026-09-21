"""SQLite persistence for rag: schema v2, incremental publish.

Schema v2 replaces "a version owns rows" with "a version lists (doc_id,
pipeline_fp) pairs" — chunks become one immutable, fingerprint-scoped
table, publish becomes a bounded diff transaction, and a settings change
is carried by the same loop that ingests today's docs.

See ``docs/rag/05-incremental-design.md`` for the full design.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from storage import FtsTable, SqliteClient

from .contract import CanonicalDoc, Chunk
from .versions import pipeline_fp

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
    ingest_state     TEXT NOT NULL DEFAULT 'staged',
    chunked_with_fp  TEXT,
    chunk_count      INTEGER NOT NULL DEFAULT 0
)
""",
    """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id           TEXT PRIMARY KEY,
    pipeline_fp        TEXT NOT NULL,
    doc_id             TEXT NOT NULL REFERENCES documents(doc_id),
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
    content_hash       TEXT NOT NULL
)
""",
    "CREATE INDEX IF NOT EXISTS chunks_doc_fp ON chunks(doc_id, pipeline_fp)",
    """
CREATE TABLE IF NOT EXISTS snapshot_docs (
    corpus_version INTEGER NOT NULL,
    doc_id         TEXT NOT NULL,
    pipeline_fp    TEXT NOT NULL,
    PRIMARY KEY (corpus_version, doc_id)
)
""",
    "CREATE INDEX IF NOT EXISTS snapshot_docs_doc ON snapshot_docs(doc_id)",
    """
CREATE TABLE IF NOT EXISTS inferred (
    chunk_id   TEXT NOT NULL,
    prompt_ver TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    keywords   TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, prompt_ver)
)
""",
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
                           quality, page_count, created_at)
                       VALUES (:doc_id, :sha, :path, :extractor, :reviewed,
                               :decision, :flags, :quality, :pages, :created)
                       ON CONFLICT(doc_id) DO UPDATE SET
                           source_sha256=excluded.source_sha256,
                           source_path=excluded.source_path,
                           extractor=excluded.extractor,
                           reviewed=excluded.reviewed,
                           publish_decision=excluded.publish_decision,
                           risk_flags=excluded.risk_flags,
                           quality=excluded.quality,
                           page_count=excluded.page_count,
                           created_at=excluded.created_at"""),
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
                },
            )
            db.commit()

    # -- staging -------------------------------------------------------------

    def stage_document(self, doc: CanonicalDoc, chunks: list[Chunk], fp: str) -> None:
        """Insert chunks (idempotent), flip doc to staged, record fp+count."""
        with self.client.session() as db:
            for c in chunks:
                db.execute(
                    text("""INSERT INTO chunks (chunk_id, pipeline_fp, doc_id,
                               chunk_type, is_parent, parent_chunk_id, section_path,
                               page_start, page_end, block_keys, text,
                               structured_payload, trust_level, content_hash)
                           VALUES (:chunk_id, :fp, :doc_id, :chunk_type, :is_parent,
                                   :parent_chunk_id, :section_path, :page_start,
                                   :page_end, :block_keys, :text, :payload,
                                   :trust_level, :content_hash)
                           ON CONFLICT(chunk_id) DO NOTHING"""),
                    {
                        "chunk_id": c.chunk_id,
                        "fp": fp,
                        "doc_id": c.doc_id,
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
                        "content_hash": c.content_hash,
                    },
                )
            n_children = sum(1 for c in chunks if not c.is_parent)
            db.execute(
                text("""UPDATE documents SET ingest_state = 'staged',
                           chunked_with_fp = :fp, chunk_count = :n
                       WHERE doc_id = :doc_id"""),
                {"fp": fp, "n": n_children, "doc_id": doc.doc_id},
            )
            db.commit()

    def next_version(self) -> int:
        with self.client.session() as db:
            row = db.execute(
                text("SELECT COALESCE(MAX(corpus_version), 0) FROM snapshots")
            ).one()
            return int(row[0]) + 1

    # -- publish -------------------------------------------------------------

    def publish_diff(self, representations: dict[str, str]) -> dict[str, Any]:
        """Incremental publish: copy base manifest ± staged/retracted, diff FTS."""
        fp = pipeline_fp()
        with self.client.session() as db:
            active = self._active_in(db)
            v_new = (active or 0) + 1

            # step 2-3: copy base manifest
            if active is not None:
                db.execute(
                    text(
                        """INSERT INTO snapshot_docs (corpus_version, doc_id, pipeline_fp)
                           SELECT :v_new, doc_id, pipeline_fp FROM snapshot_docs
                           WHERE corpus_version = :active"""
                    ),
                    {"v_new": v_new, "active": active},
                )

            # step 4: add staged docs with current fp
            staged = db.execute(
                text("""SELECT doc_id FROM documents
                       WHERE ingest_state = 'staged' AND chunked_with_fp = :fp"""),
                {"fp": fp},
            ).fetchall()
            for row in staged:
                db.execute(
                    text("""INSERT OR REPLACE INTO snapshot_docs
                           (corpus_version, doc_id, pipeline_fp)
                           VALUES (:v, :doc_id, :fp)"""),
                    {"v": v_new, "doc_id": row[0], "fp": fp},
                )

            # step 5: remove retracted docs
            retracted = db.execute(
                text(
                    """SELECT doc_id FROM documents WHERE ingest_state = 'retracted'"""
                ),
                {},
            ).fetchall()
            retracted_ids = {r[0] for r in retracted}
            if retracted_ids:
                placeholders = ", ".join(f":r{i}" for i in range(len(retracted_ids)))
                params = {f"r{i}": did for i, did in enumerate(retracted_ids)}
                params["v"] = v_new
                db.execute(
                    text(
                        f"DELETE FROM snapshot_docs "  # nosec B608
                        f"WHERE corpus_version = :v AND doc_id IN ({placeholders})"
                    ),
                    params,
                )

            # step 6-8: FTS diff
            # get old manifest's (doc_id, fp) pairs
            old_pairs: set[tuple[str, str]] = set()
            if active is not None:
                old_rows = db.execute(
                    text("""SELECT doc_id, pipeline_fp FROM snapshot_docs
                           WHERE corpus_version = :active"""),
                    {"active": active},
                ).fetchall()
                old_pairs = {(r[0], r[1]) for r in old_rows}

            # get new manifest's (doc_id, fp) pairs
            new_rows = db.execute(
                text("""SELECT doc_id, pipeline_fp FROM snapshot_docs
                       WHERE corpus_version = :v"""),
                {"v": v_new},
            ).fetchall()
            new_pairs = {(r[0], r[1]) for r in new_rows}

            # upsert FTS for newly listed pairs
            added = new_pairs - old_pairs
            for doc_id, doc_fp in added:
                chunk_rows = db.execute(
                    text("""SELECT rowid, chunk_id, section_path, text
                           FROM chunks WHERE doc_id = :doc_id AND pipeline_fp = :fp
                           AND is_parent = 0"""),
                    {"doc_id": doc_id, "fp": doc_fp},
                ).fetchall()
                for cr in chunk_rows:
                    inf = self._load_inferred(db, cr.chunk_id)
                    self.fts.upsert(
                        db,
                        cr.rowid,
                        {
                            "title": inf.get("title", "")
                            or cr.section_path.split(" > ")[-1],
                            "body": cr.text,
                            "section_path": cr.section_path,
                            "keywords": inf.get("keywords", ""),
                            "summary": inf.get("summary", ""),
                        },
                    )

            # delete FTS for retracted/replaced pairs
            removed = old_pairs - new_pairs
            for doc_id, doc_fp in removed:
                chunk_rows = db.execute(
                    text("""SELECT rowid FROM chunks
                           WHERE doc_id = :doc_id AND pipeline_fp = :fp"""),
                    {"doc_id": doc_id, "fp": doc_fp},
                ).fetchall()
                for cr in chunk_rows:
                    db.execute(
                        text("DELETE FROM chunks_fts WHERE rowid = :rid"),
                        {"rid": cr[0]},
                    )

            # step 9: snapshot row
            doc_count = len(new_pairs)
            chunk_count_row = db.execute(
                text("""SELECT COUNT(*) FROM snapshot_docs sd
                       JOIN chunks c ON c.doc_id = sd.doc_id
                           AND c.pipeline_fp = sd.pipeline_fp
                       WHERE sd.corpus_version = :v"""),
                {"v": v_new},
            ).one()
            chunk_count = int(chunk_count_row[0])
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
                    "v": v_new,
                    "at": published_at,
                    "docs": doc_count,
                    "chunks": chunk_count,
                    "repr": json.dumps(representations),
                },
            )
            db.execute(
                text("""INSERT INTO meta (key, value) VALUES ('active_version', :v)
                       ON CONFLICT(key) DO UPDATE SET value = :v"""),
                {"v": str(v_new)},
            )

            # flip staged docs to live
            db.execute(
                text("""UPDATE documents SET ingest_state = 'live'
                       WHERE ingest_state = 'staged' AND chunked_with_fp = :fp"""),
                {"fp": fp},
            )
            db.commit()

        return {
            "corpus_version": v_new,
            "doc_count": doc_count,
            "chunk_count": chunk_count,
            "staged_added": len(added),
            "retracted_removed": len(removed),
        }

    def _load_inferred(self, db: Any, chunk_id: str) -> dict[str, str]:
        """Load the latest inferred row for a chunk (any prompt_ver)."""
        row = db.execute(
            text("""SELECT title, keywords, summary FROM inferred
                   WHERE chunk_id = :cid ORDER BY created_at DESC LIMIT 1"""),
            {"cid": chunk_id},
        ).first()
        if not row:
            return {}
        return {"title": row[0], "keywords": row[1], "summary": row[2]}

    def _active_in(self, db: Any) -> int | None:
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

    # -- retraction ----------------------------------------------------------

    def retract(self, doc_id: str) -> None:
        """Mark a doc for removal at the next publish."""
        with self.client.session() as db:
            db.execute(
                text("""UPDATE documents SET ingest_state = 'retracted'
                       WHERE doc_id = :doc_id"""),
                {"doc_id": doc_id},
            )
            db.commit()

    # -- gc ------------------------------------------------------------------

    def gc(self, keep: int = 3) -> dict[str, int]:
        """Delete chunks/inferred/embeddings referenced by no retained snapshot;
        prune snapshot_docs older than the N most recent."""
        with self.client.session() as db:
            # find retained versions
            versions = db.execute(
                text("""SELECT corpus_version FROM snapshots
                       ORDER BY corpus_version DESC LIMIT :keep"""),
                {"keep": keep},
            ).fetchall()
            retained = {r[0] for r in versions}
            if not retained:
                return {
                    "chunks_deleted": 0,
                    "inferred_deleted": 0,
                    "snapshots_pruned": 0,
                }

            # delete snapshot_docs older than retained
            min_retained = min(retained)
            pruned = db.execute(
                text("""DELETE FROM snapshot_docs WHERE corpus_version < :min"""),
                {"min": min_retained},
            )
            # delete chunks not in any retained snapshot
            placeholders = ", ".join(f":v{i}" for i in range(len(retained)))
            params = {f"v{i}": v for i, v in enumerate(retained)}
            deleted_chunks = db.execute(
                text(
                    f"DELETE FROM chunks WHERE NOT EXISTS ("  # nosec B608
                    f"  SELECT 1 FROM snapshot_docs sd WHERE sd.doc_id = chunks.doc_id"
                    f"  AND sd.pipeline_fp = chunks.pipeline_fp"
                    f"  AND sd.corpus_version IN ({placeholders})"
                    f")"
                ),
                params,
            )
            # delete inferred not referencing any live chunk
            deleted_inferred = db.execute(
                text("""DELETE FROM inferred WHERE NOT EXISTS (
                           SELECT 1 FROM chunks WHERE chunks.chunk_id = inferred.chunk_id
                       )"""),
                {},
            )
            # delete old snapshot rows
            db.execute(
                text("""DELETE FROM snapshots WHERE corpus_version < :min"""),
                {"min": min_retained},
            )
            db.commit()
        return {
            "chunks_deleted": deleted_chunks.rowcount,  # type: ignore[attr-defined]
            "inferred_deleted": deleted_inferred.rowcount,  # type: ignore[attr-defined]
            "snapshots_pruned": pruned.rowcount,  # type: ignore[attr-defined]
        }

    # -- reads ----------------------------------------------------------------

    def chunks_in(self, version: int) -> list[Chunk]:
        with self.client.session() as db:
            rows = (
                db.execute(
                    text("""SELECT c.* FROM chunks c
                           JOIN snapshot_docs sd ON sd.doc_id = c.doc_id
                               AND sd.pipeline_fp = c.pipeline_fp
                           WHERE sd.corpus_version = :v"""),
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
                    f"SELECT c.chunk_id FROM chunks c "  # nosec B608
                    f"JOIN snapshot_docs sd ON sd.doc_id = c.doc_id "
                    f"AND sd.pipeline_fp = c.pipeline_fp "
                    f"WHERE sd.corpus_version = :v "
                    f"AND c.is_parent = 0 AND {clauses}"
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
            states = db.execute(
                text(
                    "SELECT ingest_state, COUNT(*) FROM documents "
                    "GROUP BY ingest_state"
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
                "documents_by_state": {r[0]: r[1] for r in states},
                "active_version": self._active_in(db),
                "pipeline_fp": pipeline_fp(),
                "snapshots": [list(r) for r in snaps],
            }

    def dispose(self) -> None:
        self.client.dispose()
