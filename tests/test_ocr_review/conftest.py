"""Shared fixtures: a tiny but real PDF + a contract document over it."""

from __future__ import annotations

import fitz  # PyMuPDF
import pytest

from ocr_backend.contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
from storage import sha256_hex


def make_pdf(path, pages: int = 2) -> dict[int, tuple[int, int]]:
    """Create a minimal PDF; returns {page_index: (rendered_px_w, h)} at
    zoom 1, which is the pixel grid the contract will declare."""
    doc = fitz.open()
    sizes: dict[int, tuple[int, int]] = {}
    for i in range(pages):
        page = doc.new_page(width=300 + i, height=400 + i)
        page.insert_text((40, 60), f"Machine text for page {i}", fontsize=12)
        pix = page.get_pixmap()
        sizes[i] = (pix.width, pix.height)
    doc.save(path)
    doc.close()
    return sizes


def make_doc(pdf_path, sizes: dict[int, tuple[int, int]]) -> OcrDocument:
    pages = [
        OcrPage(
            page_index=i,
            width=w,
            height=h,
            blocks=[
                OcrBlock(
                    id=0,
                    kind=BlockKind.paragraph,
                    raw_label="text",
                    content=f"Machine text for page {i}",
                    content_format=ContentFormat.text,
                    bbox=(40.0, 48.0, 240.0, 66.0),
                    order=1,
                    score=0.9,
                ),
                OcrBlock(
                    id=1,
                    kind=BlockKind.header,
                    raw_label="header",
                    content="bogue",
                    content_format=ContentFormat.text,
                    bbox=(10.0, 5.0, 90.0, 20.0),
                    order=None,
                    score=0.4,
                ),
            ],
        )
        for i, (w, h) in sorted(sizes.items())
    ]
    return OcrDocument(
        source=OcrSource(
            kind="pdf",
            path=str(pdf_path),
            sha256=sha256_hex(pdf_path.read_bytes()),
            page_count=len(pages),
        ),
        backend=OcrBackendInfo(
            name="paddleocr_vl",
            library_version="3.7.0",
            model="PaddleOCR-VL-1.6-0.9B",
            pipeline_version="v1.6",
            options={},
        ),
        created_at="2026-09-21T12:00:00+08:00",
        pages=pages,
    )


@pytest.fixture
def bundle(tmp_path):
    """An inbox-style bundle: source pdf + contract json, side by side."""
    pdf = tmp_path / "scan.pdf"
    sizes = make_pdf(pdf)
    doc = make_doc(pdf, sizes)
    json_path = tmp_path / "scan.ocr.json"
    json_path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    return {"pdf": pdf, "json": json_path, "doc": doc, "dir": tmp_path}


@pytest.fixture
def doc_pair(bundle):
    """(contract document, source pdf) — enough for pure-function tests."""
    return bundle["doc"], bundle["pdf"]
