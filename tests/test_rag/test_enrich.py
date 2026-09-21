"""Enrich as an independent background projection: persistence, mixed state,
and the corpus-level driver.

The old ``--enrich`` flag on ``build``/``ingest`` is gone; enrich is now
``rag enrich``, a separate step that scans unenriched child chunks, calls
the LLM, and writes each result straight to the ``inferred`` table.
"""

from __future__ import annotations

import asyncio

from rag.contract import Chunk, ChunkType
from rag.enrich import LlmEnricher
from rag.store import RagStore
from rag.versions import PROMPT_VER


def _chunk(cid: str, doc_id: str = "d1", text: str = "年假需要审批。") -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=doc_id,
        chunk_type=ChunkType.prose,
        is_parent=False,
        parent_chunk_id=None,
        section_path="1 年假 > 1.1 审批",
        text=text,
        page_span=(0, 0),
        block_keys=[f"0:{cid}"],
        content_hash="h",
    )


class FakeClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def chat_json(
        self, prompt, system_prompt="", *, schema=None, model=None, temperature=None
    ):
        self.calls += 1
        if schema is not None:
            return schema.model_validate(self.payload)
        return self.payload


def test_write_inferred_persists(store: RagStore) -> None:
    store.write_inferred(
        "c1",
        PROMPT_VER,
        title="年假审批",
        keywords="年假 审批",
        summary="关于年假审批的规定",
    )
    loaded = store._load_inferred(_raw_session(store), "c1")
    assert loaded["title"] == "年假审批"
    assert loaded["keywords"] == "年假 审批"


def test_write_inferred_idempotent(store: RagStore) -> None:
    store.write_inferred("c1", PROMPT_VER, title="v1", keywords="k1", summary="s1")
    store.write_inferred("c1", PROMPT_VER, title="v2", keywords="k2", summary="s2")
    loaded = store._load_inferred(_raw_session(store), "c1")
    assert loaded["title"] == "v2"


def test_unenriched_child_chunk_ids_empty_when_no_active(store: RagStore) -> None:
    assert store.unenriched_child_chunk_ids(PROMPT_VER) == []


def test_enricher_with_store_persists(store: RagStore) -> None:
    chunk = _chunk("c1")
    client = FakeClient({"title": "年假", "keywords": ["审批", "规定"], "summary": "s"})
    enricher = LlmEnricher(client=client, store=store)  # type: ignore[arg-type]
    asyncio.run(enricher.enrich([chunk]))

    loaded = store._load_inferred(_raw_session(store), "c1")
    assert loaded["title"] == "年假"
    assert loaded["keywords"] == "审批 规定"
    assert loaded["summary"] == "s"
    assert chunk.inferred == {}


def test_enricher_skips_already_enriched(store: RagStore) -> None:
    store.write_inferred("c1", PROMPT_VER, title="existing", keywords="", summary="")
    chunk = _chunk("c1")
    client = FakeClient({"title": "new", "keywords": [], "summary": ""})
    enricher = LlmEnricher(client=client, store=store)  # type: ignore[arg-type]
    asyncio.run(enricher.enrich([chunk]))
    assert client.calls == 0

    loaded = store._load_inferred(_raw_session(store), "c1")
    assert loaded["title"] == "existing"


def test_enricher_skips_parent_chunks(store: RagStore) -> None:
    parent = _chunk("c1")
    parent.is_parent = True
    client = FakeClient({"title": "t", "keywords": [], "summary": ""})
    enricher = LlmEnricher(client=client, store=store)  # type: ignore[arg-type]
    asyncio.run(enricher.enrich([parent]))
    assert client.calls == 0


def test_enrich_corpus_with_fake_client(built) -> None:
    """Full enrich loop with a fake LLM: unenriched -> persisted -> FTS refreshed."""
    store, report = built
    assert report["chunked_docs"] > 0

    pending = store.unenriched_child_chunk_ids(PROMPT_VER)
    assert len(pending) > 0

    chunk_ids = [cid for cid, _ in pending[:2]]
    doc_ids = sorted({did for _, did in pending[:2]})

    import json

    from sqlalchemy import text

    chunks = []
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
            chunks.append(
                Chunk(
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
            )

    client = FakeClient(
        {"title": "测试标题", "keywords": ["测试"], "summary": "测试摘要"}
    )
    enricher = LlmEnricher(
        client=client,  # type: ignore[arg-type]
        store=store,
        max_chunks=len(chunks),
    )
    asyncio.run(enricher.enrich(chunks))

    assert client.calls == len(chunks)

    for cid in chunk_ids:
        loaded = store._load_inferred(_raw_session(store), cid)
        assert loaded["title"] == "测试标题"
        assert loaded["keywords"] == "测试"

    from rag.versions import pipeline_fp

    refreshed = store.refresh_fts_for_docs(doc_ids, pipeline_fp())
    assert refreshed > 0

    pending_after = store.unenriched_child_chunk_ids(PROMPT_VER)
    enriched_ids = set(chunk_ids)
    remaining_ids = {cid for cid, _ in pending_after}
    assert enriched_ids.isdisjoint(remaining_ids)

    store.dispose()


def test_mixed_enrich_state(built) -> None:
    """Some chunks enriched, some not — FTS must work for both."""
    store, _ = built
    from rag.versions import pipeline_fp

    pending = store.unenriched_child_chunk_ids(PROMPT_VER)
    assert len(pending) >= 2

    first_id = pending[0][0]
    store.write_inferred(
        first_id, PROMPT_VER, title="已标注", keywords="标注", summary="已标注的chunk"
    )

    refreshed = store.refresh_fts_for_docs([pending[0][1]], pipeline_fp())
    assert refreshed > 0

    remaining = store.unenriched_child_chunk_ids(PROMPT_VER)
    remaining_ids = {cid for cid, _ in remaining}
    assert first_id not in remaining_ids

    store.dispose()


def _raw_session(store: RagStore):
    """Return a session handle for _load_inferred (it takes a db param)."""
    return store.client.session().__enter__()
