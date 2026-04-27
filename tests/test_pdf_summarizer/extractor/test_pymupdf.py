import pytest

from pdf_summarizer.extractor.pymupdf import PyMuPDFBackend
from pdf_summarizer.models import Document


@pytest.mark.asyncio
async def test_pymupdf_extract(sample_pdf_path):
    backend = PyMuPDFBackend()
    doc = await backend.extract(sample_pdf_path)
    assert isinstance(doc, Document)
    assert doc.total_pages == 1
    assert len(doc.pages[0].text) > 0
    assert "map-reduce" in doc.total_text


@pytest.mark.asyncio
async def test_pymupdf_multi_page(multi_page_pdf_path):
    backend = PyMuPDFBackend()
    doc = await backend.extract(multi_page_pdf_path)
    assert doc.total_pages == 5
    assert all(len(p.text) > 0 for p in doc.pages)


@pytest.mark.asyncio
async def test_pymupdf_empty_pdf(empty_pdf_path):
    backend = PyMuPDFBackend()
    with pytest.raises(ValueError, match="No extractable text"):
        await backend.extract(empty_pdf_path)
