"""Bridge tests: ``ocr_backend`` contract -> summarizer ``Document``, model-free.

``PaddleOCRVLBackend.parse`` is replaced with canned ``OcrDocument``s — the
bridge's whole job is to not care how the document was produced. The real
end-to-end run (engine + scanned PDF) lives in the ``OCR_LIVE``-gated tests
of ``test_ocr_backend`` and in ``shell_scipts/verify_paddle_vl_16.py``.
"""

import pytest

from ocr_backend.backends.paddleocr_vl import PaddleOCRVLBackend
from ocr_backend.contract import (
    BlockKind,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
from pdf_summarizer.extractor.paddle_vl import PaddleVLBackend


def make_block(
    id: int,
    kind: BlockKind,
    content: str,
    order: int | None = None,
    raw_label: str = "",
) -> OcrBlock:
    return OcrBlock(
        id=id,
        kind=kind,
        raw_label=raw_label or str(kind),
        content=content,
        content_format="text",
        bbox=(0.0, 0.0, 1.0, 1.0),
        order=order,
    )


def make_document(pages: list[OcrPage]) -> OcrDocument:
    return OcrDocument(
        source=OcrSource(
            kind="pdf", path="fake.pdf", sha256="0" * 64, page_count=len(pages)
        ),
        backend=OcrBackendInfo(
            name="paddleocr_vl",
            library_version="test",
            model="test",
            pipeline_version="v1.6",
            options={},
        ),
        created_at="2026-09-21T00:00:00+08:00",
        pages=pages,
    )


@pytest.fixture
def patch_parse(monkeypatch):
    def _patch(document: OcrDocument) -> None:
        monkeypatch.setattr(PaddleOCRVLBackend, "parse", lambda self, source: document)

    return _patch


@pytest.mark.asyncio
async def test_projects_in_reading_order_and_skips_furniture(patch_parse):
    page = OcrPage(
        page_index=0,
        width=100,
        height=200,
        blocks=[
            make_block(0, BlockKind.header, "journal header", raw_label="header"),
            make_block(1, BlockKind.paragraph, "second", order=2),
            make_block(2, BlockKind.paragraph, "first", order=1),
            make_block(3, BlockKind.page_number, "1", raw_label="number"),
            make_block(4, BlockKind.paragraph, "third", order=3),
        ],
    )
    patch_parse(make_document([page]))

    doc = await PaddleVLBackend().extract("fake.pdf")

    assert doc.filename == "fake.pdf"
    assert len(doc.pages) == 1
    # Reading order (not list order), furniture skipped, blocks split by \n\n.
    assert doc.pages[0].page_number == 1  # page_index 0 -> 1-based
    assert doc.pages[0].text == "first\n\nsecond\n\nthird"


@pytest.mark.asyncio
async def test_blank_and_furniture_only_pages_are_skipped(patch_parse):
    pages = [
        OcrPage(
            page_index=0,
            width=100,
            height=200,
            blocks=[make_block(0, BlockKind.paragraph, "A", order=1)],
        ),
        OcrPage(page_index=1, width=100, height=200, blocks=[]),
        OcrPage(
            page_index=2,
            width=100,
            height=200,
            blocks=[
                make_block(0, BlockKind.footer, "confidential", raw_label="footer")
            ],
        ),
        OcrPage(
            page_index=3,
            width=100,
            height=200,
            blocks=[make_block(0, BlockKind.paragraph, "D", order=1)],
        ),
    ]
    patch_parse(make_document(pages))

    doc = await PaddleVLBackend().extract("fake.pdf")

    # Page numbers keep pointing at the source pages, gaps included.
    assert [(p.page_number, p.text) for p in doc.pages] == [(1, "A"), (4, "D")]


@pytest.mark.asyncio
async def test_no_extractable_text_raises(patch_parse):
    patch_parse(
        make_document([OcrPage(page_index=0, width=100, height=200, blocks=[])])
    )

    with pytest.raises(ValueError, match="No extractable text"):
        await PaddleVLBackend().extract("fake.pdf")
