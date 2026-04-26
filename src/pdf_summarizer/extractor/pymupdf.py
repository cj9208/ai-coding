import asyncio
import unicodedata

import fitz

from ..models import Document, Page
from .base import ExtractorBackend


class PyMuPDFBackend(ExtractorBackend):
    async def extract(self, filepath: str) -> Document:
        return await asyncio.to_thread(self._extract_sync, filepath)

    def _extract_sync(self, filepath: str) -> Document:
        try:
            doc = fitz.open(filepath)
        except fitz.FileDataError as e:
            raise ValueError(f"Failed to open PDF (corrupted or invalid): {e}")
        except RuntimeError as e:
            raise ValueError(f"Failed to open PDF (possibly encrypted): {e}")

        pages = []
        try:
            for i, page in enumerate(doc):
                text = page.get_text("text")
                text = unicodedata.normalize("NFKC", text)
                text = " ".join(text.split())
                if text.strip():
                    pages.append(Page(page_number=i + 1, text=text))
        finally:
            doc.close()

        if not pages:
            raise ValueError("No extractable text found in PDF")

        return Document(pages=pages, filename=filepath)
