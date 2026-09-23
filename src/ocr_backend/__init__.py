"""OCR backend layer — one canonical output contract, pluggable adapters.

Why this package exists (full rationale in ``contract.py``): the repo wants a
single place that turns "a PDF or image file" into structured, JSON-native
output, so that every consumer (pdf_summarizer today, file_manager later) reads
the same shape and no consumer depends on a specific OCR engine. PaddleOCR-VL
1.6 is the first adapter (``backends/paddleocr_vl.py``).

Layout::

    ocr_backend/
      contract.py       # OcrDocument & friends — data only, import freely
      render.py         # projections: page_text / document_markdown
      models.py         # local model snapshots: cache/ocr_backend/models/<name>/
      cli.py            # `ocr-backend download <name>` — one-command provisioning
      backends/         # adapters, each importing its engine lazily

The contract and render layers are light (pydantic only); a backend drags in
its engine (``paddleocr`` + ``paddlex`` live behind the ``ocr`` frontend extra
plus a ``paddle-cpu`` / ``paddle-gpu`` engine extra), so backends are imported
on demand, never from this ``__init__``.

What belongs here: the contract, its projections, and thin adapters that map
an engine's native result into it. What does not: engine-specific output
structures leaking into consumers, storage, or pipeline orchestration —
consumers compose those on top.

Usage::

    from ocr_backend import page_text
    from ocr_backend.backends.paddleocr_vl import (
        PaddleOCRVLBackend,
        PaddleOCRVLConfig,
    )

    backend = PaddleOCRVLBackend(PaddleOCRVLConfig(device="gpu"))
    doc = backend.parse("scan.pdf")                       # OcrDocument
    text = "\\n\\n".join(page_text(page) for page in doc.pages)
"""

from .contract import (
    SCHEMA_VERSION,
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
from .models import model_dir
from .render import document_markdown, page_text

__all__ = [
    "SCHEMA_VERSION",
    "BlockKind",
    "ContentFormat",
    "OcrBackendInfo",
    "OcrBlock",
    "OcrDocument",
    "OcrPage",
    "OcrSource",
    "document_markdown",
    "model_dir",
    "page_text",
]
