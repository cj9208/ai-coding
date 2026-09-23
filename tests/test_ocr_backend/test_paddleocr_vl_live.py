"""Live end-to-end OCR test — real model, real machine. Skipped by default.

Enable with ``OCR_LIVE=1`` once the local model snapshot exists
(``cache/ocr_backend/models/paddleocr-vl-1.6/``, provisioned by
``ocr-backend download paddleocr-vl-1.6``; see AGENTS.md). The backend resolves
it automatically, so the test also proves that path. Uses the single-page
verification image to keep the run short; run ``shell_scipts/verify_paddle_vl_16.py``
for the full six-item checklist. Set ``OCR_DEVICE=cpu`` to force CPU.
"""

import json
import os

import pytest

from ocr_backend.backends.paddleocr_vl import PaddleOCRVLBackend, PaddleOCRVLConfig
from ocr_backend.models import model_dir
from ocr_backend.render import page_text
from utils.paths import REPO_ROOT

MODEL_DIR = model_dir("paddleocr-vl-1.6")
MATERIAL = REPO_ROOT / "data" / "ocr_backend" / "verify" / "materials" / "page1.png"

pytestmark = pytest.mark.skipif(
    not os.getenv("OCR_LIVE") or not MODEL_DIR.is_dir() or not MATERIAL.is_file(),
    reason="OCR_LIVE not set (or model snapshot / material missing) — skipping live OCR test",
)


def test_parse_image_end_to_end():
    backend = PaddleOCRVLBackend(
        PaddleOCRVLConfig(device=os.getenv("OCR_DEVICE") or None)
    )
    try:
        doc = backend.parse(MATERIAL)
    finally:
        backend.close()

    assert doc.schema_version == "1.0"
    assert doc.source.kind == "image"
    assert doc.source.page_count == 1
    assert doc.backend.name == "paddleocr_vl"
    assert doc.backend.pipeline_version == "v1.6"
    assert "PaddleOCR-VL" in doc.backend.model
    page = doc.pages[0]
    assert page.width > 0 and page.height > 0
    assert page.blocks
    assert page_text(page).strip()
    # The document must survive the JSON boundary consumers will use.
    dumped = json.loads(json.dumps(doc.model_dump(mode="json")))
    assert dumped["pages"][0]["blocks"]
