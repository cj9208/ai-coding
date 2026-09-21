"""Golden-sample mapping tests — real PaddleOCR-VL 1.6 output, no model loading.

Every ``fixtures/page_*_res.json`` is a sidecar JSON dumped by the live
verification run (2026-09-21, paddleocr 3.7.0 / PaddleOCR-VL-1.6-0.9B, see
``docs/ocr-backend-design.md`` §5.3). The upgrade flow documented in §6 runs on
exactly these tests: new model → new sidecars → mapping diff shows every change.
"""

import json
from pathlib import Path

import pytest

from ocr_backend.backends import OcrBackend
from ocr_backend.backends.paddleocr_vl import (
    PaddleOCRVLBackend,
    PaddleOCRVLConfig,
    map_page,
)
from ocr_backend.contract import BlockKind, ContentFormat

FIXTURES = Path(__file__).parent / "fixtures"


def load_res(name: str) -> dict:
    """A fixture is a sidecar JSON: either the plain res dict or ``{"res": ...}``."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data.get("res", data)


# --- golden pages -----------------------------------------------------------


def test_text_page_golden():
    page = map_page(load_res("page_text_res.json"))

    assert page.page_index == 0
    assert (page.width, page.height) == (1191, 1684)
    assert [b.raw_label for b in page.blocks] == [
        "header",
        "paragraph_title",
        "text",
        "text",
        "footer",
        "number",
    ]
    assert [b.kind for b in page.blocks] == [
        BlockKind.header,
        BlockKind.title,
        BlockKind.paragraph,
        BlockKind.paragraph,
        BlockKind.footer,
        BlockKind.page_number,
    ]
    # block_order is 1-based over the main flow; furniture carries None.
    assert [b.order for b in page.blocks] == [None, 1, 2, 3, None, None]
    assert all(b.content_format is ContentFormat.text for b in page.blocks)
    assert [b.id for b in page.blocks] == [0, 1, 2, 3, 4, 5]
    assert page.blocks[1].content == "PaddleOCR-VL 1.6 Output Verification"
    assert page.blocks[1].bbox == (95.0, 129.0, 825.0, 171.0)
    # Scores are the layout boxes' (recognition has none) matched by IoU.
    assert page.blocks[1].score == pytest.approx(0.7884, abs=1e-4)
    assert page.blocks[2].score == pytest.approx(0.9405, abs=1e-4)
    assert page.blocks[0].score == pytest.approx(0.7331, abs=1e-4)


def test_table_page_golden():
    page = map_page(load_res("page_table_res.json"))

    header, table, text, number = page.blocks
    assert header.kind is BlockKind.header
    assert table.kind is BlockKind.table
    assert table.raw_label == "table"
    assert table.content_format is ContentFormat.html
    assert table.content.startswith("<table>") and table.content.endswith("</table>")
    assert table.order is None  # tables sit outside the numbered flow (verified)
    assert table.score == pytest.approx(0.9611, abs=1e-4)
    assert text.order == 1
    assert number.kind is BlockKind.page_number


def test_formula_page_golden():
    page = map_page(load_res("page_formula_res.json"))

    formula, formula_number, text, number = page.blocks
    assert formula.kind is BlockKind.formula
    assert formula.content_format is ContentFormat.latex
    # Delimiters are stripped by the adapter; render re-adds them.
    assert formula.content == r"\mathrm{L}=1/2\uprho v^{2}\mathrm{~S~C~}_{-}\mathrm{L}"
    assert formula.order == 1
    assert formula_number.kind is BlockKind.caption
    assert formula_number.raw_label == "formula_number"
    assert formula_number.content == "(1)"
    assert text.order == 3
    assert number.kind is BlockKind.page_number


def test_figure_page_golden():
    page = map_page(load_res("page_figure_res.json"))

    assert [b.kind for b in page.blocks] == [
        BlockKind.caption,
        BlockKind.figure,
        BlockKind.caption,
        BlockKind.page_number,
    ]
    # Captions/figures are outside the reading flow — all orders are None.
    assert [b.order for b in page.blocks] == [None, None, None, None]
    chart = page.blocks[1]
    assert chart.raw_label == "chart"
    assert chart.content == ""
    assert chart.score == pytest.approx(0.6303, abs=1e-4)
    assert page.blocks[0].content == "Accuracy by version (synthetic)"


def test_markdown_mode_strips_heading_prefix():
    """format_block_content=True run: the engine decorates titles with ``### ``;
    the contract renders headings from kind+raw_label instead."""
    page = map_page(load_res("page_text_markdown_res.json"), format_block_content=True)

    assert page.blocks[1].content == "PaddleOCR-VL 1.6 Output Verification"
    assert page.blocks[1].content_format is ContentFormat.markdown
    assert page.blocks[2].content_format is ContentFormat.markdown


def test_nolayout_page_golden():
    """use_layout_detection=False: one whole-page ``ocr`` block, no geometry
    source, null page_index (image input) — the adapter stays total."""
    page = map_page(load_res("page_nolayout_res.json"), fallback_index=7)

    assert page.page_index == 7
    assert (page.width, page.height) == (1241, 1754)
    assert len(page.blocks) == 1
    block = page.blocks[0]
    assert block.raw_label == "ocr"
    assert block.kind is BlockKind.paragraph
    assert block.content_format is ContentFormat.markdown
    assert block.order == 1
    assert block.bbox == (0.0, 0.0, 1241.0, 1754.0)
    assert block.score is None  # no layout detection -> no scores


def test_blank_page_golden():
    page = map_page(load_res("page_blank_res.json"))

    assert page.blocks == []
    assert (page.width, page.height) == (1191, 1684)


def test_image_only_page_golden():
    page = map_page(load_res("page_image_only_res.json"))

    assert len(page.blocks) == 1
    block = page.blocks[0]
    assert block.kind is BlockKind.figure
    assert block.raw_label == "image"
    assert block.content == ""
    assert block.bbox == (157.0, 398.0, 1003.0, 1238.0)
    assert block.score == pytest.approx(0.9494, abs=1e-4)


# --- mapping robustness -----------------------------------------------------


def test_unknown_label_falls_back_to_other():
    res = {
        "parsing_res_list": [
            {
                "block_label": "some_future_label",
                "block_content": "x",
                "block_bbox": [0, 0, 5, 5],
                "block_id": 0,
                "block_order": None,
            }
        ]
    }
    block = map_page(res).blocks[0]
    assert block.kind is BlockKind.other
    assert block.raw_label == "some_future_label"


def test_degenerate_block_does_not_crash():
    block = map_page({"parsing_res_list": [{}]}).blocks[0]
    assert block.raw_label == ""
    assert block.kind is BlockKind.other
    assert block.content == ""
    assert block.bbox == (0.0, 0.0, 0.0, 0.0)
    assert block.id == 0
    assert block.score is None


def test_score_none_without_overlapping_layout_box():
    res = {
        "layout_det_res": {
            "boxes": [{"coordinate": [500, 500, 600, 600], "score": 0.9}]
        },
        "parsing_res_list": [
            {
                "block_label": "text",
                "block_content": "far away",
                "block_bbox": [0, 0, 10, 10],
                "block_id": 0,
                "block_order": 1,
            }
        ],
    }
    assert map_page(res).blocks[0].score is None


# --- backend seam (never loads the model) -----------------------------------


def test_backend_satisfies_protocol_and_capabilities():
    backend = PaddleOCRVLBackend()
    assert isinstance(backend, OcrBackend)
    assert backend.name == "paddleocr_vl"
    assert backend.capabilities == frozenset(
        {"block_bbox", "reading_order", "table_html", "formula_latex"}
    )
    nolayout = PaddleOCRVLBackend(PaddleOCRVLConfig(use_layout_detection=False))
    assert nolayout.capabilities == frozenset()


def test_backend_is_lazy_and_validates_source():
    backend = PaddleOCRVLBackend()
    with pytest.raises(FileNotFoundError):
        backend.parse(FIXTURES / "no_such_file.pdf")
    # Constructing and failing on a missing file must not have touched the engine.
    assert backend._pipeline is None
