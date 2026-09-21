"""Structure reconstruction + publish-gate unit tests."""

from __future__ import annotations

from ocr_backend.contract import BlockKind, ContentFormat
from rag.contract import CanonicalDoc, PublishDecision, SourceInfo, TrustInfo
from rag.structure import rebuild_section_paths
from rag.validate import (
    _check_bbox_overlap,
    _check_char_distribution_anomaly,
    _check_formula_parseable,
    _check_ngram_entropy,
    _check_page_number_monotonic,
    _check_table_cell_consistency,
    assess,
)

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


# Phase 1: expanded deterministic checks


def test_page_number_monotonic_clean():
    """Sequential page numbers should not flag."""
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
        block(2, BlockKind.page_number, "1", order=2),
    ]
    assert not _check_page_number_monotonic(blocks)


def test_page_number_monotonic_decreasing():
    """Page numbers that decrease should flag."""
    blocks = [
        block(0, BlockKind.page_number, "3", order=0),
        block(1, BlockKind.page_number, "1", order=1),
    ]
    assert _check_page_number_monotonic(blocks)


def test_page_number_monotonic_single():
    """A single page number should not flag (need >= 2 to compare)."""
    blocks = [block(0, BlockKind.page_number, "1", order=0)]
    assert not _check_page_number_monotonic(blocks)


def test_page_number_monotonic_in_assess():
    """Non-monotonic page numbers should add the flag and trigger quarantine."""
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
        block(2, BlockKind.page_number, "5", order=2),
        block(3, BlockKind.page_number, "2", order=3),
    ]
    trust = assess(_canonical([blocks]))
    assert "page_number_non_monotonic" in trust.risk_flags


def test_char_distribution_clean():
    """Normal mixed text should not flag."""
    assert not _check_char_distribution_anomaly(
        "这是一段正常的中文文本，包含一些English words和123数字。"
    )


def test_char_distribution_all_symbols():
    """Text that is mostly symbols should flag."""
    assert _check_char_distribution_anomaly("@#$%^&*()!@#$%^&*()!@#$%^&*()" * 3)


def test_char_distribution_no_text():
    """Text with zero CJK and zero Latin should flag."""
    assert _check_char_distribution_anomaly("1234567890 !@#$%^&*()" * 3)


def test_char_distribution_short_text():
    """Very short text should not flag (not enough signal)."""
    assert not _check_char_distribution_anomaly("@#$")


def test_ngram_entropy_clean():
    """Normal text with varied n-grams should not flag."""
    assert not _check_ngram_entropy(
        "这是一段正常的文本，包含各种不同的内容和词汇组合。"
    )


def test_ngram_entropy_repeating():
    """Repeating patterns should flag (low entropy)."""
    assert _check_ngram_entropy("啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊")


def test_ngram_entropy_short():
    """Very short text should not flag."""
    assert not _check_ngram_entropy("短文本")


def test_bbox_overlap_clean():
    """Non-overlapping blocks should not flag."""
    blocks = [
        block(0, BlockKind.paragraph, "text1", order=0),
        block(1, BlockKind.paragraph, "text2", order=1),
    ]
    # conftest.block creates non-overlapping bboxes (y increases with order)
    assert _check_bbox_overlap(blocks) == 0


def test_bbox_overlap_detected():
    """Overlapping blocks should be counted."""
    from ocr_backend.contract import OcrBlock

    b1 = OcrBlock(
        id=0,
        kind=BlockKind.paragraph,
        raw_label="paragraph",
        content="text1",
        content_format=ContentFormat.text,
        bbox=(10.0, 10.0, 100.0, 100.0),
        order=0,
        score=0.9,
    )
    b2 = OcrBlock(
        id=1,
        kind=BlockKind.paragraph,
        raw_label="paragraph",
        content="text2",
        content_format=ContentFormat.text,
        bbox=(50.0, 50.0, 150.0, 150.0),
        order=1,
        score=0.9,
    )
    assert _check_bbox_overlap([b1, b2]) == 1


def test_bbox_overlap_in_assess():
    """Overlapping bboxes should add the flag."""
    from ocr_backend.contract import OcrBlock

    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
    ]
    # Replace block 1 with one that overlaps block 0
    blocks[1] = OcrBlock(
        id=1,
        kind=BlockKind.paragraph,
        raw_label="paragraph",
        content="正文内容" * 20,
        content_format=ContentFormat.text,
        bbox=(10.0, 10.0, 200.0, 200.0),
        order=1,
        score=0.9,
    )
    trust = assess(_canonical([blocks]))
    assert "bbox_overlap" in trust.risk_flags


def test_table_cell_consistency_clean():
    """A table with consistent column counts should not flag."""
    from ocr_backend.contract import OcrBlock

    table = OcrBlock(
        id=0,
        kind=BlockKind.table,
        raw_label="table",
        content="| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |",
        content_format=ContentFormat.markdown,
        bbox=(0, 0, 100, 100),
        order=0,
        score=0.9,
    )
    assert not _check_table_cell_consistency(table)


def test_table_cell_consistency_inconsistent():
    """A table with varying column counts should flag."""
    from ocr_backend.contract import OcrBlock

    table = OcrBlock(
        id=0,
        kind=BlockKind.table,
        raw_label="table",
        content="| A | B |\n|---|---|\n| 1 | 2 | 3 |\n| 4 |",
        content_format=ContentFormat.markdown,
        bbox=(0, 0, 100, 100),
        order=0,
        score=0.9,
    )
    assert _check_table_cell_consistency(table)


def test_table_cell_consistency_html_skipped():
    """HTML tables should not be checked (different structure)."""
    from ocr_backend.contract import OcrBlock

    table = OcrBlock(
        id=0,
        kind=BlockKind.table,
        raw_label="table",
        content="<table><tr><td>1</td></tr></table>",
        content_format=ContentFormat.html,
        bbox=(0, 0, 100, 100),
        order=0,
        score=0.9,
    )
    assert not _check_table_cell_consistency(table)


def test_formula_parseable_clean():
    """A well-formed LaTeX formula should not flag."""
    from ocr_backend.contract import OcrBlock

    formula = OcrBlock(
        id=0,
        kind=BlockKind.formula,
        raw_label="formula",
        content="E = mc^{2}",
        content_format=ContentFormat.latex,
        bbox=(0, 0, 100, 100),
        order=0,
        score=0.9,
    )
    assert not _check_formula_parseable(formula)


def test_formula_parseable_unbalanced():
    """A formula with unbalanced braces should flag."""
    from ocr_backend.contract import OcrBlock

    formula = OcrBlock(
        id=0,
        kind=BlockKind.formula,
        raw_label="formula",
        content="E = mc^{2",
        content_format=ContentFormat.latex,
        bbox=(0, 0, 100, 100),
        order=0,
        score=0.9,
    )
    assert _check_formula_parseable(formula)


def test_formula_parseable_empty():
    """An empty formula block should flag."""
    from ocr_backend.contract import OcrBlock

    formula = OcrBlock(
        id=0,
        kind=BlockKind.formula,
        raw_label="formula",
        content="  ",
        content_format=ContentFormat.latex,
        bbox=(0, 0, 100, 100),
        order=0,
        score=0.9,
    )
    assert _check_formula_parseable(formula)


def test_empty_blocks_flag():
    """Documents with many empty blocks should flag."""
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
        block(2, BlockKind.paragraph, "", order=2),
        block(3, BlockKind.paragraph, "", order=3),
        block(4, BlockKind.paragraph, "", order=4),
    ]
    trust = assess(_canonical([blocks]))
    assert "empty_blocks" in trust.risk_flags
    assert "empty_block_ratio" in trust.quality


def test_empty_blocks_few_ok():
    """A couple of empty blocks among many should not flag."""
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 30, order=1),
        block(2, BlockKind.paragraph, "更多内容" * 30, order=2),
        block(3, BlockKind.paragraph, "还有更多" * 30, order=3),
        block(4, BlockKind.paragraph, "继续填充" * 30, order=4),
        block(5, BlockKind.paragraph, "最后一段" * 30, order=5),
        block(6, BlockKind.paragraph, "额外内容" * 30, order=6),
        block(7, BlockKind.paragraph, "补充说明" * 30, order=7),
        block(8, BlockKind.paragraph, "结尾部分" * 30, order=8),
        block(9, BlockKind.paragraph, " truly final " * 30, order=9),
        block(10, BlockKind.paragraph, "", order=10),
    ]
    trust = assess(_canonical([blocks]))
    assert "empty_blocks" not in trust.risk_flags


def test_combined_flags_quarantine():
    """Two high-severity flags should quarantine."""
    from ocr_backend.contract import OcrBlock

    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
        block(2, BlockKind.page_number, "5", order=2),
        block(3, BlockKind.page_number, "2", order=3),
    ]
    # Add an overlapping block to get bbox_overlap (high severity)
    blocks.append(
        OcrBlock(
            id=4,
            kind=BlockKind.paragraph,
            raw_label="paragraph",
            content="重叠内容" * 10,
            content_format=ContentFormat.text,
            bbox=(10.0, 10.0, 200.0, 200.0),
            order=4,
            score=0.9,
        )
    )
    trust = assess(_canonical([blocks]))
    assert "page_number_non_monotonic" in trust.risk_flags
    assert "bbox_overlap" in trust.risk_flags
    assert trust.publish_decision == PublishDecision.quarantine


def test_quality_metrics_populated():
    """New quality metrics should be populated in the TrustInfo."""
    blocks = [
        block(0, BlockKind.title, "1 标题", order=0),
        block(1, BlockKind.paragraph, "正文内容" * 20, order=1),
    ]
    trust = assess(_canonical([blocks]))
    assert "chars_per_page" in trust.quality
    assert "ordered_block_ratio" in trust.quality
    assert "mean_block_score" in trust.quality
