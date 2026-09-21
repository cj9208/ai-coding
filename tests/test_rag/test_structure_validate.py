"""Structure reconstruction + publish-gate unit tests."""

from __future__ import annotations

from ocr_backend.contract import BlockKind
from rag.contract import CanonicalDoc, PublishDecision, SourceInfo, TrustInfo
from rag.structure import rebuild_section_paths
from rag.validate import assess

from .conftest import LEAVE_DOC_BLOCKS, block, make_document


def _canonical(blocks_pages) -> CanonicalDoc:
    doc = make_document("x", blocks_pages)
    return CanonicalDoc(
        doc_id=CanonicalDoc.make_doc_id(doc.source.sha256),
        source=SourceInfo(
            kind="pdf",
            path="x.pdf",
            sha256=doc.source.sha256,
            extractor="t",
            page_count=1,
            created_at=doc.created_at,
        ),
        document=doc,
    )


def test_section_paths_follow_heading_stack():
    cdoc = _canonical([LEAVE_DOC_BLOCKS])
    paths = rebuild_section_paths(cdoc.document)
    assert paths["0:0"] == "1 年假规定"
    assert paths["0:1"] == "1 年假规定"
    assert paths["0:2"] == "1 年假规定 > 1.1 审批流程"
    assert paths["0:4"] == "1 年假规定 > 1.1 审批流程"


def test_furniture_blocks_excluded():
    paths = rebuild_section_paths(_canonical([LEAVE_DOC_BLOCKS]).document)
    assert "0:5" not in paths  # header
    assert "0:6" not in paths  # page number


def test_clean_document_passes(leave_doc):
    assert leave_doc.trust.publish_decision == PublishDecision.pass_
    assert leave_doc.section_paths  # structure was rebuilt during fetch


def test_no_text_fails():
    empty = [block(0, BlockKind.other, "  ", order=0)]
    trust = assess(_canonical([empty]))
    assert trust.publish_decision == PublishDecision.fail


def test_garbled_table_quarantines_without_review():
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
        block(2, BlockKind.table, "完全不是表格结构", score=0.1, order=2),
        block(3, BlockKind.table, "也不是表格", score=0.1, order=3),
    ]
    trust = assess(_canonical([blocks]))
    assert trust.publish_decision == PublishDecision.quarantine
    assert "table_unstructured" in trust.risk_flags


def test_reviewed_document_upgrades_out_of_quarantine():
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
        block(2, BlockKind.table, "低质量一", score=0.1, order=2),
        block(3, BlockKind.table, "低质量二", score=0.1, order=3),
    ]
    cdoc = _canonical([blocks])
    cdoc.source.reviewed = True
    cdoc.trust = assess(cdoc)
    assert cdoc.trust.publish_decision == PublishDecision.pass_with_warning


def test_missing_source_hash_fails():
    cdoc = _canonical([LEAVE_DOC_BLOCKS])
    cdoc.source.sha256 = ""
    trust = assess(cdoc)
    assert trust.publish_decision == PublishDecision.fail
    # a TrustInfo built by hand keeps the contract honest: defaults pass
    assert TrustInfo().publish_decision == PublishDecision.pass_
