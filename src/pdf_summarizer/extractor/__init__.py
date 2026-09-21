from ..models import Document
from .base import ExtractorBackend, auto_extract
from .paddle_vl import PaddleVLBackend
from .pymupdf import PyMuPDFBackend


def create_extractor(name: str = "auto") -> ExtractorBackend:
    if name == "pymupdf":
        return PyMuPDFBackend()
    elif name == "paddleocr-vl":
        return PaddleVLBackend()
    elif name == "auto":
        return _AutoBackend([PyMuPDFBackend(), PaddleVLBackend()])
    else:
        raise ValueError(
            f"Unknown extractor backend: {name!r}. "
            f"Use 'auto', 'pymupdf', or 'paddleocr-vl'."
        )


class _AutoBackend(ExtractorBackend):
    def __init__(self, backends: list[ExtractorBackend]):
        self._backends = backends

    async def extract(self, filepath: str) -> Document:
        return await auto_extract(filepath, self._backends)
