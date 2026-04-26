import asyncio

from ..models import Document, Page
from .base import ExtractorBackend


class PaddleOCRBackend(ExtractorBackend):
    def __init__(self):
        self._ocr = None

    def _get_ocr(self):
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError:
                raise ImportError(
                    "paddleocr is required for OCR extraction. "
                    "Install with: pip install paddleocr paddlepaddle"
                )
            self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        return self._ocr

    async def extract(self, filepath: str) -> Document:
        return await asyncio.to_thread(self._extract_sync, filepath)

    def _extract_sync(self, filepath: str) -> Document:
        try:
            from pdf2image import convert_from_path
        except ImportError:
            raise ImportError(
                "pdf2image is required for PaddleOCR extraction. "
                "Install with: pip install pdf2image"
            )

        ocr = self._get_ocr()
        images = convert_from_path(filepath)

        pages = []
        for i, img in enumerate(images):
            result = ocr.ocr(img, cls=True)
            if result and result[0]:
                text = " ".join(line[1][0] for line in result[0])
                text = " ".join(text.split())
                if text.strip():
                    pages.append(Page(page_number=i + 1, text=text))

        if not pages:
            raise ValueError("No extractable text found in PDF")

        return Document(pages=pages, filename=filepath)
