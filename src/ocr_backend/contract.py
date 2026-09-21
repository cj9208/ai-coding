"""The canonical OCR output contract — the stable seam between OCR backends and
their consumers.

Why this package exists: the repo has more than one place that wants to read
documents (pdf_summarizer, file_manager, future agents), and the natural end
state is "some OCR backend turns a PDF/image into a structured result". If every
consumer talks to a backend's native output directly, then upgrading the backend
(PaddleOCR-VL 1.6 -> 1.7 -> something else) becomes a cross-project rewrite.
Instead this package owns one versioned document shape: backends are thin
adapters that map their native result into it (one-way), consumers read only
it, and the two sides can upgrade independently because ``schema_version`` +
``capabilities`` + ``kind`` describe exactly what a document promises.

Design rules, in one place so they cannot drift:

- Block-level only. PaddleOCR-VL's recognition unit is a layout block; there is
  no stable line/word source, so we do not model one (add fields later if a
  backend ever provides it — that is a compatible change).
- JSON-native, zero special types. Everything must round-trip through
  ``model_dump(mode="json")`` / ``model_validate`` with no numpy, no PIL, no
  datetime objects — same rule as ``storage`` and ``llm_client``, data settles
  into plain types before it flows.
- Raw labels are preserved (``raw_label``) so a backend renaming its labels
  never breaks the contract; consumers branch on ``kind``, not on labels.
- Coordinates are pixel-space with an explicit space declaration
  (``coordinate_space``) plus per-page ``width``/``height``. Normalizing to
  [0, 1] is one division for a consumer; guessing pixels back from normalized
  values is lossy.
- Unknown fields are ignored on load (pydantic default), so adding fields to a
  later minor revision stays readable by older code.

What stays OUT: rendering policy (see ``render.py`` — projections are plain
functions over the contract), backend specifics (see ``backends/``), and any
notion of where documents get stored. This module is data only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

SCHEMA_VERSION = "1.0"


class BlockKind(StrEnum):
    """Normalized block type — what a consumer branches on.

    A small closed set on purpose: backend labels are many and shifting
    (``content``, ``vision_footnote``, ...), kinds are few and stable. New
    backend labels map onto an existing kind, or fall back to ``other``.
    """

    title = "title"  # type: ignore[assignment]  # shadows str.title; fine at runtime
    paragraph = "paragraph"
    table = "table"
    formula = "formula"
    figure = "figure"
    caption = "caption"
    list_item = "list_item"
    header = "header"
    footer = "footer"
    page_number = "page_number"
    footnote = "footnote"
    other = "other"


class ContentFormat(StrEnum):
    """How to interpret ``OcrBlock.content``."""

    text = "text"
    markdown = "markdown"
    html = "html"
    latex = "latex"


class OcrBlock(BaseModel):
    """One layout block, the atomic unit of the contract."""

    id: int
    """Backend block index (Paddle ``block_id``), unique within the page."""

    kind: BlockKind
    """Normalized type; the mapping is the adapter's job."""

    raw_label: str
    """Backend's original label, verbatim — the buffer against renaming."""

    content: str
    """Block content (body text / table HTML / formula LaTeX)."""

    content_format: ContentFormat
    """How to read ``content``; adapters normalize the wrapping form."""

    bbox: tuple[float, float, float, float]
    """x1, y1, x2, y2 in this page's pixel grid, origin top-left."""

    order: int | None = None
    """Reading order within the page; ``None`` means the backend deliberately
    places this block outside the main flow (captions, figures, furniture)."""

    score: float | None = None
    """Best available quality score. For Paddle this is the layout-detection
    score — NOT a recognition confidence (VL recognition has none)."""


class OcrPage(BaseModel):
    """One page's blocks plus the raster geometry their bboxes live in."""

    page_index: int
    """0-based page number."""

    width: int
    """Pixel width of this page's raster = the bbox coordinate ceiling."""

    height: int

    blocks: list[OcrBlock]


class OcrSource(BaseModel):
    """Where the document came from, with a hash for dedup/provenance."""

    kind: Literal["pdf", "image"]
    path: str
    sha256: str
    page_count: int


class OcrBackendInfo(BaseModel):
    """Which backend and model produced this — written for diffability:
    two runs of different versions can be compared document-to-document."""

    name: str
    library_version: str
    model: str
    pipeline_version: str
    options: dict[str, Any]


class OcrDocument(BaseModel):
    """The full output of one OCR run over one source file."""

    schema_version: str = SCHEMA_VERSION
    coordinate_space: Literal["image_px_top_left"] = "image_px_top_left"
    source: OcrSource
    backend: OcrBackendInfo
    created_at: str
    """ISO 8601 with timezone offset (kept a string to stay JSON-native)."""
    pages: list[OcrPage]
