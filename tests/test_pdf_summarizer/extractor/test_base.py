import builtins

import pytest

from pdf_summarizer.extractor import create_extractor
from pdf_summarizer.extractor.paddle_ocr import PaddleOCRBackend
from pdf_summarizer.extractor.pymupdf import PyMuPDFBackend


@pytest.mark.asyncio
async def test_create_extractor_pymupdf():
    backend = create_extractor("pymupdf")
    assert isinstance(backend, PyMuPDFBackend)


@pytest.mark.asyncio
async def test_create_extractor_auto():
    backend = create_extractor("auto")
    assert backend is not None


def test_create_extractor_invalid():
    with pytest.raises(ValueError, match="Unknown extractor backend"):
        create_extractor("nonexistent")


def test_missing_paddleocr_explains_uv_extra(monkeypatch):
    original_import = builtins.__import__

    def import_without_paddleocr(name, *args, **kwargs):
        if name == "paddleocr":
            raise ImportError("paddleocr is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_paddleocr)

    with pytest.raises(ImportError, match=r"uv sync --extra ocr"):
        PaddleOCRBackend()._get_ocr()


@pytest.mark.asyncio
async def test_auto_extract_falls_back(sample_pdf_path):
    backend = create_extractor("auto")
    doc = await backend.extract(sample_pdf_path)
    assert doc.total_pages == 1


@pytest.mark.asyncio
async def test_auto_extract_with_corrupted_file(tmp_path):
    bad_path = tmp_path / "corrupted.pdf"
    bad_path.write_bytes(b"not a pdf")
    backend = create_extractor("auto")
    with pytest.raises(ValueError):
        await backend.extract(str(bad_path))
