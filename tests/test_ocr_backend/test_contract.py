"""Contract tests — JSON-native round trip and the JSON Schema snapshot.

Nothing here loads a model or imports a backend: the contract must be usable
(and testable) by consumers that never installed an OCR engine.
"""

import json
from pathlib import Path

from ocr_backend import SCHEMA_VERSION
from ocr_backend.contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _document() -> OcrDocument:
    return OcrDocument(
        source=OcrSource(kind="pdf", path="demo.pdf", sha256="0" * 64, page_count=1),
        backend=OcrBackendInfo(
            name="paddleocr_vl",
            library_version="3.7.0",
            model="PaddleOCR-VL-1.6-0.9B",
            pipeline_version="v1.6",
            options={"use_layout_detection": True},
        ),
        created_at="2026-09-21T12:00:00+08:00",
        pages=[
            OcrPage(
                page_index=0,
                width=1191,
                height=1684,
                blocks=[
                    OcrBlock(
                        id=0,
                        kind=BlockKind.title,
                        raw_label="paragraph_title",
                        content="Title",
                        content_format=ContentFormat.text,
                        bbox=(95.0, 129.0, 825.0, 171.0),
                        order=1,
                        score=0.7884,
                    ),
                    OcrBlock(
                        id=1,
                        kind=BlockKind.figure,
                        raw_label="chart",
                        content="",
                        content_format=ContentFormat.text,
                        bbox=(214.0, 297.0, 987.0, 920.0),
                    ),
                ],
            )
        ],
    )


def test_json_roundtrip_preserves_everything():
    doc = _document()
    dumped = json.loads(json.dumps(doc.model_dump(mode="json")))
    assert OcrDocument.model_validate(dumped) == doc
    assert dumped["schema_version"] == SCHEMA_VERSION == "1.0"
    assert dumped["coordinate_space"] == "image_px_top_left"
    # None order/score must survive the round trip (no falsy coercion).
    assert dumped["pages"][0]["blocks"][1]["order"] is None
    assert dumped["pages"][0]["blocks"][1]["score"] is None


def test_dump_is_plain_json_types():
    # json.dumps raises on anything non-JSON-native (numpy, datetime, ...).
    dumped = _document().model_dump(mode="json")
    assert json.dumps(dumped)
    assert dumped["pages"][0]["blocks"][0]["bbox"] == [95.0, 129.0, 825.0, 171.0]


def test_schema_snapshot():
    """The exported JSON Schema is part of the contract (consumers may generate
    code from it), so any change must be an intentional snapshot edit."""
    stored = json.loads(
        (FIXTURES / "ocr_document.schema.json").read_text(encoding="utf-8")
    )
    assert OcrDocument.model_json_schema() == stored
