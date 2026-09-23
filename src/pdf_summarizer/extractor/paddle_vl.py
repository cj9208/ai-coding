"""PaddleOCR-VL extractor — a bridge from ``ocr_backend`` to this project.

The engine output contract lives in ``src/ocr_backend`` (``OcrDocument``:
page -> blocks); this project's map-reduce pipeline still speaks plain text
per page (``Document`` / ``Page``). The bridge is the single place that
projects one onto the other — blocks in reading order, page furniture
skipped (``ocr_backend.render.page_text``) — see
``docs/ocr-backend-design.md`` §4.2 and §7 step 3.

Deliberately thin: no engine imports, no model handling, no config surface
beyond passing a ``PaddleOCRVLConfig`` through — where the weights live is
``ocr_backend``'s business (``ocr_backend.models`` prefers the repo snapshot
under ``cache/ocr_backend/models/`` automatically). The backend is lazy — the
model loads on the first ``extract`` call, not at construction.
"""

import asyncio

from ocr_backend.backends.paddleocr_vl import PaddleOCRVLBackend, PaddleOCRVLConfig
from ocr_backend.render import page_text

from ..models import Document, Page
from .base import ExtractorBackend


class PaddleVLBackend(ExtractorBackend):
    """OCR fallback for scanned PDFs and images, via ``ocr_backend``."""

    def __init__(self, config: PaddleOCRVLConfig | None = None):
        self._backend = PaddleOCRVLBackend(config or PaddleOCRVLConfig())

    async def extract(self, filepath: str) -> Document:
        return await asyncio.to_thread(self._extract_sync, filepath)

    def _extract_sync(self, filepath: str) -> Document:
        ocr_document = self._backend.parse(filepath)
        pages = []
        for ocr_page in ocr_document.pages:
            text = page_text(ocr_page).strip()
            if text:
                pages.append(Page(page_number=ocr_page.page_index + 1, text=text))
        if not pages:
            raise ValueError("No extractable text found in PDF")
        return Document(pages=pages, filename=filepath)
