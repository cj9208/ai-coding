"""Projection tests — ``page_text`` / ``document_markdown`` over hand-built
contract objects (no fixtures, no backend, no model)."""

from ocr_backend.contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
from ocr_backend.render import PAGE_FURNITURE, document_markdown, page_text


def _block(
    id: int,
    kind: BlockKind,
    content: str,
    *,
    raw_label: str | None = None,
    order: int | None = None,
    fmt: ContentFormat = ContentFormat.text,
) -> OcrBlock:
    return OcrBlock(
        id=id,
        kind=kind,
        raw_label=raw_label or kind.value,
        content=content,
        content_format=fmt,
        bbox=(0.0, 0.0, 10.0, 10.0),
        order=order,
    )


def _page(blocks: list[OcrBlock]) -> OcrPage:
    return OcrPage(page_index=0, width=100, height=100, blocks=blocks)


def _doc(pages: list[OcrPage]) -> OcrDocument:
    return OcrDocument(
        source=OcrSource(
            kind="pdf", path="demo.pdf", sha256="0" * 64, page_count=len(pages)
        ),
        backend=OcrBackendInfo(
            name="test",
            library_version="0",
            model="test",
            pipeline_version="v0",
            options={},
        ),
        created_at="2026-09-21T12:00:00+08:00",
        pages=pages,
    )


def test_page_text_orders_blocks_and_skips_furniture():
    page = _page(
        [
            _block(0, BlockKind.header, "running head"),
            _block(1, BlockKind.paragraph, "second body", order=2),
            _block(2, BlockKind.title, "the title", order=1),
            _block(3, BlockKind.footer, "printed at"),
            _block(4, BlockKind.page_number, "page 1"),
        ]
    )
    assert page_text(page) == "the title\n\nsecond body"


def test_page_text_keeps_unordered_blocks_last():
    page = _page(
        [
            _block(0, BlockKind.caption, "figure 1: legend"),
            _block(1, BlockKind.paragraph, "body", order=1),
            _block(2, BlockKind.formula, r"E=mc^2", order=2, fmt=ContentFormat.latex),
        ]
    )
    # order=None sorts after the numbered flow, keeping its list position.
    assert page_text(page) == "body\n\nE=mc^2\n\nfigure 1: legend"


def test_page_text_custom_skip_and_empty_blocks():
    page = _page(
        [
            _block(0, BlockKind.header, "running head"),
            _block(1, BlockKind.paragraph, "  "),
            _block(2, BlockKind.caption, ""),
            _block(3, BlockKind.paragraph, " body ", order=1),
        ]
    )
    # Custom skip keeps the header; empty/blank blocks are dropped. The header
    # has order=None, so it still sorts after the numbered flow.
    assert page_text(page, skip=frozenset()) == "body\n\nrunning head"


def test_document_markdown_renders_titles_formulas_and_tables():
    page = _page(
        [
            _block(0, BlockKind.title, "Doc", raw_label="doc_title", order=1),
            _block(1, BlockKind.title, "Section", raw_label="paragraph_title", order=2),
            _block(2, BlockKind.paragraph, "Plain paragraph.", order=3),
            _block(
                3,
                BlockKind.formula,
                r"\alpha + \beta",
                order=4,
                fmt=ContentFormat.latex,
            ),
            _block(
                4,
                BlockKind.table,
                "<table><tr><td>1</td></tr></table>",
                order=5,
                fmt=ContentFormat.html,
            ),
            _block(5, BlockKind.figure, "", order=6),
            _block(6, BlockKind.header, "head"),
        ]
    )
    md = document_markdown(_doc([page]))
    assert "# Doc" in md
    assert "## Section" in md
    assert "Plain paragraph." in md
    assert "$$\n\\alpha + \\beta\n$$" in md
    assert "<table><tr><td>1</td></tr></table>" in md
    assert "head" not in md  # furniture skipped by default
    assert md.index("# Doc") < md.index("## Section") < md.index("Plain paragraph.")


def test_document_markdown_pages_join_and_idempotent_latex():
    already_wrapped = _block(
        0, BlockKind.formula, "$$x$$", order=1, fmt=ContentFormat.latex
    )
    page = _page([already_wrapped])
    md = document_markdown(_doc([page, page]))
    # Wrapped input stays as-is (never doubled), pages joined by a blank line.
    assert md == "$$x$$\n\n$$x$$"


def test_furniture_default_set_is_the_documented_one():
    assert PAGE_FURNITURE == frozenset(
        {BlockKind.header, BlockKind.footer, BlockKind.page_number}
    )
