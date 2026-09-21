"""Backend adapters — one module per OCR engine, imported on demand.

Each adapter maps its engine's native result *one-way* into the canonical
contract (``ocr_backend.contract``): engine types, numpy arrays, and version
strings stop at this boundary. Adapters must not be imported from the package
``__init__`` because each one drags in a heavy engine dependency — import the
concrete module (``from ocr_backend.backends.paddleocr_vl import ...``) when
you actually need it.

What a backend has to look like is the tiny protocol below — deliberately a
duck-typed ``Protocol``, not an ABC hierarchy: adapters just implement it,
consumers just check it, and nothing else (no async, no instance pools, no
plugin registry) is part of the seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..contract import OcrDocument


@runtime_checkable
class OcrBackend(Protocol):
    """The seam a consumer may rely on, whatever engine sits behind it.

    One method plus two descriptors. Async callers wrap ``parse`` in
    ``asyncio.to_thread`` at their call site; backends stay synchronous.
    """

    name: str
    """Stable backend id (e.g. ``"paddleocr_vl"``) for metadata and logs."""

    capabilities: frozenset[str]
    """What *this configuration* actually guarantees, e.g.
    ``{"block_bbox", "reading_order", "table_html", "formula_latex"}``.
    Consumers branch on these, never on backend names or engine versions.
    May be empty in degraded modes (PaddleOCR-VL without layout detection
    returns a single whole-page markdown block: no per-block geometry, no
    reading order)."""

    def parse(self, source: str | Path) -> OcrDocument:
        """Run the backend over one PDF or image file and map the result."""
        ...
