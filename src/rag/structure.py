"""Structure reconstruction: recover a section hierarchy from flat blocks.

The OcrDocument contract keeps blocks per page with kinds and reading order
but has no notion of sections (CH03_01's structure-reconstruction step is
not done by the OCR backend). This module rebuilds it deterministically:
title blocks form a heading stack (level from numeric prefixes like
``2.1`` when present, else depth-1), every other block inherits the current
path. Heuristic and conservative on purpose — a wrong path only costs
retrieval context quality, while guessing content would violate the trust
chain; ambiguous structure should surface via ocr-review, not be invented.
"""

from __future__ import annotations

import re

from ocr_backend.contract import BlockKind, OcrBlock, OcrDocument

from .contract import block_key

_NUM_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)[\s.、)]")
_MAX_TITLE_LEN = 60

FURNITURE_KINDS = frozenset({BlockKind.header, BlockKind.footer, BlockKind.page_number})
"""Blocks excluded from the main flow entirely (same set render.page_text skips)."""


def _heading_level(title: str) -> int:
    m = _NUM_PREFIX_RE.match(title)
    if m:
        return min(m.group(1).count(".") + 1, 6)
    return 1


def _truncate(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:_MAX_TITLE_LEN]


def reading_order_blocks(doc: OcrDocument) -> list[tuple[int, OcrBlock]]:
    """(page_index, block) pairs in reconstruction order: ordered blocks by
    ``order``, unordered ones after them in page order (they sit outside the
    main flow — captions, figures)."""
    pairs: list[tuple[int, OcrBlock]] = []
    for page in doc.pages:
        main = [b for b in page.blocks if b.order is not None]
        rest = [b for b in page.blocks if b.order is None]
        pairs.extend(
            (page.page_index, b) for b in sorted(main, key=lambda b: b.order or 0)
        )
        pairs.extend((page.page_index, b) for b in rest)
    return pairs


def rebuild_section_paths(doc: OcrDocument) -> dict[str, str]:
    """``block_key -> " > "-joined heading path`` for every non-furniture block."""
    paths: dict[str, str] = {}
    stack: list[str] = []
    for page_index, block in reading_order_blocks(doc):
        key = block_key(page_index, block.id)
        if block.kind in FURNITURE_KINDS:
            continue
        if block.kind == BlockKind.title:
            level = _heading_level(block.content)
            stack = stack[: level - 1] + [_truncate(block.content)]
            paths[key] = " > ".join(stack)
            continue
        paths[key] = " > ".join(stack)
    return paths
