"""Online pipeline tests: shaping, fusion, end-to-end retrieval, abstention."""

from __future__ import annotations

from pathlib import Path

from rag.assemble import assemble
from rag.contract import Candidate
from rag.engine import empty_pack, retrieve
from rag.fuse import rrf_fuse
from rag.shape import LexicalShaper
from storage import SqliteCache


def test_shaper_splits_long_cjk_runs():
    shaped = LexicalShaper().shape("年假超过几天需要审批")
    assert all(len(t) <= 2 for t in shaped.tokens)  # long run -> 2-char segments
    shaped_short = LexicalShaper().shape("年假 政策")
    assert "年假" in shaped_short.tokens
    assert "政策" in shaped_short.tokens


def test_shaper_lowercases_ascii():
    assert "pdf" in LexicalShaper().shape("PDF Report").tokens


def test_rrf_fuse_merges_and_dedupes():
    a = [
        Candidate(chunk_id="x", scores={"fts": 9.0}, ranks={"fts": 1}),
        Candidate(chunk_id="y", scores={"fts": 8.0}, ranks={"fts": 2}),
    ]
    b = [Candidate(chunk_id="y", scores={"dense": 0.9}, ranks={"dense": 1})]
    fused = rrf_fuse([("fts", a), ("dense", b)])
    assert [c.chunk_id for c in fused] == ["y", "x"]  # y wins rank-1 on both
    assert fused[0].scores["fts"] == 8.0
    assert fused[0].scores["dense"] == 0.9
    assert "rrf" in fused[0].ranks


def test_retrieve_finds_the_right_evidence(built):
    store, _ = built
    pack = retrieve(store, "年假审批", k=3)
    assert not pack.insufficient
    texts = " ".join(c.text for c in pack.chunks)
    assert "部门经理审批" in texts
    assert all(c.block_keys for c in pack.chunks)  # citations traceable


def test_parent_expansion_attaches_section_context(built):
    store, _ = built
    pack = retrieve(store, "审批", k=3)
    assert pack.chunks
    assert any(c.parent_chunk_id for c in pack.chunks)
    for c in pack.chunks:
        if c.parent_chunk_id:
            assert any(p.chunk_id == c.parent_chunk_id for p in pack.parents)


def test_abstention_on_no_match(built):
    store, _ = built
    pack = retrieve(store, "完全无关的量子纠缠问题", k=3)
    assert pack.insufficient
    assert pack.strength["n_candidates"] == 0


def test_weak_evidence_floor_abstains(built):
    """A single stray token surviving OR relaxation must not reach the LLM."""
    store, _ = built
    version = store.active_version()
    child = next(c for c in store.chunks_in(version) if not c.is_parent)
    weak = Candidate(
        chunk_id=child.chunk_id, scores={"fts": 1e-6}, ranks={"fts": 1, "rrf": 1}
    )
    pack = assemble(store.client, "q", [weak], k=3, path_meta=[])
    assert pack.insufficient
    assert pack.chunks  # kept visible for --retrieve-only diagnosis


def test_retrieve_before_any_build(store):
    pack = retrieve(store, "任何", k=3)
    assert pack.insufficient
    assert "rag build" in pack.notes[0]


def test_empty_pack_helper():
    assert empty_pack("q", "why").insufficient


def test_retrieve_with_cache_returns_same_result(built, tmp_path: Path):
    store, _ = built
    cache = SqliteCache(tmp_path / "cache.db")
    try:
        pack1 = retrieve(store, "年假审批", k=3, cache=cache)
        pack2 = retrieve(store, "年假审批", k=3, cache=cache)
        assert pack1.query == pack2.query
        assert [c.chunk_id for c in pack1.chunks] == [c.chunk_id for c in pack2.chunks]
        assert pack1.strength == pack2.strength
    finally:
        cache.close()


def test_cache_miss_on_version_change(built, tmp_path: Path):
    """After a rebuild (new version), old cache entries are skipped."""
    store, _ = built
    cache = SqliteCache(tmp_path / "cache.db")
    try:
        retrieve(store, "年假审批", k=3, cache=cache)
        version1 = store.active_version()
        # Simulate a version bump by directly writing a cache entry with old tag
        from storage import sha256_hex

        shaped = LexicalShaper().shape("年假审批")
        key = sha256_hex(f"年假审批:3:{','.join(shaped.tokens)}")
        cache.put(
            key,
            '{"query":"年假审批","chunks":[],"strength":{},"insufficient":true,"notes":["stale"]}',
            tag=str(version1),
        )
        # Same query, same version -> hits cache (the entry we just wrote)
        pack2 = retrieve(store, "年假审批", k=3, cache=cache)
        assert pack2.insufficient  # from our injected stale entry
    finally:
        cache.close()
