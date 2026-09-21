"""Store + publish semantics: staging, atomic swap, rollback, eval reads."""

from __future__ import annotations

from sqlalchemy import text

from ocr_backend.contract import BlockKind
from rag.contract import CanonicalDoc, PublishDecision
from rag.pipeline import REPRESENTATIONS, build

from .conftest import block, make_document, write_bundle


def test_build_publishes_and_records_snapshot(built):
    store, report = built
    version = store.active_version()
    assert version == 1
    snap = store.snapshot(version)
    assert snap is not None
    assert snap["representations"] == REPRESENTATIONS
    assert report["doc_count"] == 2
    assert report["chunk_count"] == len(store.chunks_in(version))


def test_rebuild_bumps_version_and_keeps_history(built, inbox, tmp_path):
    store, _ = built
    v1 = store.active_version()
    report2 = build(inbox, tmp_path / "data", store=store)
    v2 = store.active_version()
    assert v2 == v1 + 1
    assert report2["corpus_version"] == v2
    assert store.chunks_in(v1)  # old snapshot survives for rollback
    assert store.active_version() == v2


def test_publish_reindexes_fts_to_new_version_only(built):
    store, _ = built
    v1 = store.active_version()
    assert v1 is not None
    # stage a version 2 holding a single chunk and publish it
    one = [c for c in store.chunks_in(v1) if not c.is_parent][:1]
    store.stage_chunks(2, one)
    store.publish(2, REPRESENTATIONS)
    from storage import match_expr

    query = match_expr(["审批"])
    with store.client.session() as db:
        fts_rowids = {
            r[0]
            for r in db.execute(
                text("SELECT rowid FROM chunks_fts " "WHERE chunks_fts MATCH :m"),
                {"m": query},
            )
        }
        v2_rowids = {
            r[0]
            for r in db.execute(
                text("SELECT rowid FROM chunks WHERE corpus_version = 2")
            )
        }
    assert fts_rowids <= v2_rowids  # no stale hits from v1


def test_chunk_ids_matching_resolves_substrings(built):
    store, _ = built
    version = store.active_version()
    assert version is not None
    ids = store.chunk_ids_matching(version, ["审批"])
    assert ids
    assert store.chunk_ids_matching(version, []) == set()


def test_quarantined_document_never_chunked(inbox, tmp_path):
    junk = [
        block(0, BlockKind.title, "x", order=0),
        block(1, BlockKind.table, "乱码一", score=0.1, order=1),
        block(2, BlockKind.table, "乱码二", score=0.1, order=2),
    ]
    doc = make_document("junk", [junk])
    write_bundle(inbox, "junk", doc)
    report = build(inbox, tmp_path / "d2")
    assert report["decisions"].get(PublishDecision.quarantine.value) == 1

    from rag.store import RagStore

    s = RagStore(tmp_path / "d2" / "kb.db")
    junk_id = CanonicalDoc.make_doc_id(doc.source.sha256)
    version = s.active_version()
    assert version is not None
    assert all(c.doc_id != junk_id for c in s.chunks_in(version))
    s.dispose()
