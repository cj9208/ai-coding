from __future__ import annotations

from pathlib import Path

from .base import ExtractionResult, Extractor


class PdfExtractor(Extractor):
    extensions = frozenset({"pdf"})

    def extract(self, path: Path) -> ExtractionResult:
        import fitz  # pymupdf，项目已有依赖

        doc = fitz.open(path)
        try:
            title = (doc.metadata or {}).get("title") or None
            text = "\n".join(page.get_text() for page in doc)
            return ExtractionResult(title=title, text=text or None)
        finally:
            doc.close()
