"""Chunking contract tests: atomicity, parent links, id stability."""

from __future__ import annotations

import pytest

from ocr_backend.contract import BlockKind
from rag.chunking import CHILD_CHAR_LIMIT, StructureAwareChunker
from rag.contract import CanonicalDoc, ChunkType, SourceInfo
from rag.structure import rebuild_section_paths

from .conftest import block, make_document


@pytest.fixture
def pricing_doc(acquirer, inbox):
    return acquirer.fetch(inbox / "pricing.ocr.json")


def test_table_stays_atomic(leave_doc):
    chunks = StructureAwareChunker().split(leave_doc)
    tables = [c for c in chunks if c.chunk_type is ChunkType.table]
    assert len(tables) == 1
    assert "<table>" in tables[0].text
    assert tables[0].structured_payload["formats"] == ["html"]


def test_children_carry_parent_and_traceability(leave_doc):
    chunks = StructureAwareChunker().split(leave_doc)
    children = [c for c in chunks if not c.is_parent]
    parents = {c.chunk_id for c in chunks if c.is_parent}
    for c in children:
        assert c.block_keys
        assert c.doc_id == leave_doc.doc_id
        if c.parent_chunk_id:
            assert c.parent_chunk_id in parents
    parent = next(c for c in chunks if c.is_parent)
    child_keys = {
        k
        for c in children
        if c.parent_chunk_id == parent.chunk_id
        for k in c.block_keys
    }
    assert child_keys <= set(parent.block_keys)


def test_list_items_merge_into_one_chunk(pricing_doc):
    chunks = StructureAwareChunker().split(pricing_doc)
    lists = [c for c in chunks if c.chunk_type is ChunkType.list]
    assert len(lists) == 1
    assert "学生" in lists[0].text and "年付" in lists[0].text


def test_chunk_id_is_content_addressed(leave_doc):
    split = StructureAwareChunker().split
    assert {c.chunk_id for c in split(leave_doc)} == {
        c.chunk_id for c in split(leave_doc)
    }


def test_long_prose_windowed():
    long_text = "很长的段落内容。" * (CHILD_CHAR_LIMIT // 8)
    doc = make_document(
        "long",
        [
            [
                block(0, BlockKind.title, "1 长文", order=0),
                block(1, BlockKind.paragraph, long_text, order=1),
                block(2, BlockKind.paragraph, long_text, order=2),
            ]
        ],
    )
    cdoc = CanonicalDoc(
        doc_id="dlong",
        source=SourceInfo(
            kind="pdf",
            path="l.pdf",
            sha256=doc.source.sha256,
            extractor="t",
            page_count=1,
            created_at=doc.created_at,
        ),
        document=doc,
        section_paths=rebuild_section_paths(doc),
    )
    chunks = StructureAwareChunker().split(cdoc)
    children = [c for c in chunks if not c.is_parent]
    assert len(children) >= 2  # the two 1750-char paragraphs were windowed
    assert all(len(c.text) <= CHILD_CHAR_LIMIT + 50 for c in children)
