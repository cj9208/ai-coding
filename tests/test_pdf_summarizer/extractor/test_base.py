"""Auto-chain tests. Model-free on purpose: the OCR engine's real behavior is
covered by the ``OCR_LIVE``-gated tests in ``test_ocr_backend`` (see AGENTS.md)
— here we shape the chain as a machine without the engine (``engine_less``),
which is also what the CI sees.
"""

import builtins

import pytest

from pdf_summarizer.extractor import create_extractor
from pdf_summarizer.extractor.base import ExtractorBackend, auto_extract
from pdf_summarizer.extractor.paddle_vl import PaddleVLBackend
from pdf_summarizer.extractor.pymupdf import PyMuPDFBackend


@pytest.fixture
def engine_less(monkeypatch):
    """Make ``import paddleocr`` fail, as on a machine without the OCR extra."""
    original_import = builtins.__import__

    def import_without_paddleocr(name, *args, **kwargs):
        if name == "paddleocr":
            raise ImportError("paddleocr is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_paddleocr)


@pytest.mark.asyncio
async def test_create_extractor_pymupdf():
    backend = create_extractor("pymupdf")
    assert isinstance(backend, PyMuPDFBackend)


@pytest.mark.asyncio
async def test_create_extractor_paddle_vl():
    backend = create_extractor("paddleocr-vl")
    assert isinstance(backend, PaddleVLBackend)  # lazy: no model load here


def test_create_extractor_invalid():
    with pytest.raises(ValueError, match="Unknown extractor backend"):
        create_extractor("nonexistent")


@pytest.mark.asyncio
async def test_auto_extract_falls_back(sample_pdf_path):
    # Text-layer PDF: the primary backend wins, the OCR path is never reached.
    backend = create_extractor("auto")
    doc = await backend.extract(sample_pdf_path)
    assert doc.total_pages == 1


@pytest.mark.asyncio
async def test_auto_extract_skips_missing_engine(empty_pdf_path, engine_less):
    """A missing OCR engine is skipped like any uninstalled backend; the chain
    surfaces PyMuPDF's honest 'no text' error instead of a paddleocr crash."""
    backend = create_extractor("auto")
    with pytest.raises(ValueError, match="No extractable text"):
        await backend.extract(str(empty_pdf_path))


@pytest.mark.asyncio
async def test_auto_extract_with_corrupted_file(tmp_path, engine_less):
    bad_path = tmp_path / "corrupted.pdf"
    bad_path.write_bytes(b"not a pdf")
    backend = create_extractor("auto")
    with pytest.raises(ValueError, match="corrupted or invalid"):
        await backend.extract(str(bad_path))


class _FailingBackend(ExtractorBackend):
    def __init__(self, error: Exception):
        self._error = error

    async def extract(self, filepath: str):
        raise self._error


@pytest.mark.asyncio
async def test_auto_extract_reports_primary_error():
    """The primary backend's verdict surfaces; a fallback's error is a failed
    attempt, not a better description of the problem."""
    chain = [
        _FailingBackend(ValueError("Failed to open PDF (corrupted or invalid)")),
        _FailingBackend(RuntimeError("engine noise")),
    ]
    with pytest.raises(ValueError, match="corrupted or invalid"):
        await auto_extract("whatever.pdf", chain)
