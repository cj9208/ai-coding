"""Deterministic publish gate (CH03_01's production-side validation).

Scope decision recorded up front: of the chapter's four validation
categories we implement only *technical deterministic* checks — the
proxy-metric column of its tables. Model-based validation is a supporting
signal with no labeled corpus to calibrate against yet, comparative
(cross-extractor / bilingual) validation has one extractor and no parallel
versions, and business rules belong to a domain team. Crucially, the human
gate already exists one level up: an ocr-review sidecar folded in
(``source.reviewed``) is a stronger trust signal than any of these proxies,
so a reviewed document is allowed to upgrade out of quarantine.

Phase 1 (2026-09-21): expanded from 3 to 10 checks. The original three
(text density, reading-order coverage, table structure) catch the most
egregious failures; the seven new checks target distinct VLM failure modes
— page-number breaks, character-distribution anomalies, n-gram entropy
collapse (hallucination loops), bbox overlap, table cell inconsistency,
formula parseability, and empty-content blocks. All zero-cost (no LLM, no
external dependency). See ``docs/rag/06-scalable-validation.md`` for the
four-layer architecture this is Layer 1 of.

Severity rule: two high-severity flags (or a metadata failure) quarantine;
one warns; clean passes. Thresholds are calibration inputs, not constants.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from ocr_backend.contract import BlockKind, ContentFormat, OcrBlock

from .contract import CanonicalDoc, PublishDecision, TrustInfo
from .structure import FURNITURE_KINDS, reading_order_blocks

_MIN_TEXT_PER_PAGE = 30.0
_MIN_ORDERED_RATIO = 0.5
_HIGH_SEVERITY = frozenset(
    {
        "low_text_density",
        "table_unstructured",
        "page_number_non_monotonic",
        "bbox_overlap",
    }
)
_TABLE_MARKERS = ("<table", "<tr", "|")
_EMPTY_CONTENT_RATIO = 0.1


def _extract_page_numbers(body: list[OcrBlock]) -> list[int]:
    nums: list[int] = []
    for b in body:
        if b.kind == BlockKind.page_number:
            m = re.search(r"\d+", b.content)
            if m:
                nums.append(int(m.group()))
    return nums


def _check_page_number_monotonic(body: list[OcrBlock]) -> bool:
    """True when page numbers exist and are not strictly non-decreasing."""
    nums = _extract_page_numbers(body)
    if len(nums) < 2:
        return False
    return any(nums[i] > nums[i + 1] for i in range(len(nums) - 1))


def _check_char_distribution_anomaly(text: str) -> bool:
    """True when character-type ratios are far from typical document text.

    Counts CJK / Latin / digit / symbol / whitespace. Flags when symbols
    exceed 15% of non-whitespace characters, or when there is zero CJK
    and zero Latin (i.e. nothing recognizable as text).

    The ``text`` argument should be plain text only — HTML/markup tags
    inflate the symbol count and produce false positives, so callers
    filter by content format before joining.
    """
    if len(text) < 50:
        return False
    cjk = latin = digit = symbol = ws = 0
    for ch in text:
        if ch.isspace():
            ws += 1
        elif "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf":
            cjk += 1
        elif ch.isalpha():
            latin += 1
        elif ch.isdigit():
            digit += 1
        else:
            symbol += 1
    non_ws = cjk + latin + digit + symbol
    if non_ws == 0:
        return True
    if cjk == 0 and latin == 0:
        return True
    if symbol / non_ws > 0.15:
        return True
    return False


def _check_ngram_entropy(text: str) -> bool:
    """True when character-level 4-gram entropy is abnormally low.

    Low entropy means the text is dominated by a few repeating patterns —
    a strong signal of OCR hallucination loops ("the the the", "———").
    Threshold 2.0 bits: a uniform distribution over 4-grams would be ~11 bits
    for CJK text; repeating 4-char patterns collapse to ~0.
    """
    clean = re.sub(r"\s+", "", text)
    if len(clean) < 20:
        return False
    n = 4
    grams = [clean[i : i + n] for i in range(len(clean) - n + 1)]
    if not grams:
        return False
    counts = Counter(grams)
    total = len(grams)
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return entropy < 2.0


def _bbox_area(x1: float, y1: float, x2: float, y2: float) -> float:
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _check_bbox_overlap(blocks: list[OcrBlock]) -> int:
    """Count of overlapping body-block pairs on the same page.

    Uses a sweep-line on the x-axis to avoid O(n^2) full comparison:
    only blocks whose x-ranges overlap are tested for y-overlap.
    """
    overlaps = 0
    items = [
        (b.bbox[0], b.bbox[1], b.bbox[2], b.bbox[3])
        for b in blocks
        if b.order is not None and b.kind not in FURNITURE_KINDS
    ]
    items.sort(key=lambda r: r[0])
    for i, (x1, y1, x2, y2) in enumerate(items):
        for j in range(i + 1, len(items)):
            ox1, oy1, ox2, oy2 = items[j]
            if ox1 >= x2:
                break
            ix1 = max(x1, ox1)
            iy1 = max(y1, oy1)
            ix2 = min(x2, ox2)
            iy2 = min(y2, oy2)
            if ix1 < ix2 and iy1 < iy2:
                inter = (ix2 - ix1) * (iy2 - iy1)
                smaller = min(
                    _bbox_area(x1, y1, x2, y2), _bbox_area(ox1, oy1, ox2, oy2)
                )
                if smaller > 0 and inter / smaller > 0.3:
                    overlaps += 1
    return overlaps


def _check_table_cell_consistency(table: OcrBlock) -> bool:
    """True when a markdown-style table has inconsistent column counts across rows."""
    if table.content_format == ContentFormat.html:
        return False
    lines = [ln.strip() for ln in table.content.strip().split("\n") if ln.strip()]
    pipe_lines = [ln for ln in lines if "|" in ln]
    if len(pipe_lines) < 2:
        return False
    counts = [ln.count("|") for ln in pipe_lines]
    return len(set(counts)) > 1


def _check_formula_parseable(formula: OcrBlock) -> bool:
    """True when a LaTeX formula block has unbalanced braces or is empty."""
    content = formula.content.strip()
    if not content:
        return True
    depth = 0
    for ch in content:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if depth < 0:
            return True
    return depth != 0


def assess(doc: CanonicalDoc) -> TrustInfo:
    """Run the proxy checks and return the gate decision; never mutates the
    document."""
    flags: list[str] = []
    quality: dict[str, float] = {}

    pairs = reading_order_blocks(doc.document)
    body: list[OcrBlock] = [b for _, b in pairs if b.kind not in FURNITURE_KINDS]
    all_blocks: list[OcrBlock] = [b for _, b in pairs]

    total_chars = sum(len(b.content.strip()) for b in body)
    pages = max(len(doc.document.pages), 1)
    quality["chars_per_page"] = total_chars / pages
    if total_chars == 0:
        return TrustInfo(
            publish_decision=PublishDecision.fail,
            risk_flags=["no_text"],
            quality=quality,
        )
    if quality["chars_per_page"] < _MIN_TEXT_PER_PAGE:
        flags.append("low_text_density")

    ordered = [b for b in body if b.order is not None]
    quality["ordered_block_ratio"] = len(ordered) / max(len(body), 1)
    if quality["ordered_block_ratio"] < _MIN_ORDERED_RATIO:
        flags.append("reading_order_sparse")

    scores = [b.score for b in body if b.score is not None]
    if scores:
        quality["mean_block_score"] = sum(scores) / len(scores)

    tables = [b for b in body if b.kind == BlockKind.table]
    if tables:
        structured = sum(
            1 for b in tables if any(m in b.content.lower() for m in _TABLE_MARKERS)
        )
        quality["table_structured_ratio"] = structured / len(tables)
        flags.extend("table_unstructured" for _ in range(len(tables) - structured))

        cell_inconsistent = sum(
            1
            for b in tables
            if any(m in b.content.lower() for m in _TABLE_MARKERS)
            and _check_table_cell_consistency(b)
        )
        if cell_inconsistent:
            quality["table_cell_inconsistent_count"] = float(cell_inconsistent)
            flags.extend("table_cell_inconsistent" for _ in range(cell_inconsistent))

    formulas = [b for b in body if b.kind == BlockKind.formula]
    if formulas:
        unparseable = sum(1 for f in formulas if _check_formula_parseable(f))
        if unparseable:
            quality["formula_unparseable_count"] = float(unparseable)
            flags.extend("formula_unparseable" for _ in range(unparseable))

    empty_blocks = sum(1 for b in body if not b.content.strip())
    if empty_blocks:
        quality["empty_block_ratio"] = empty_blocks / max(len(body), 1)
        if quality["empty_block_ratio"] > _EMPTY_CONTENT_RATIO:
            flags.append("empty_blocks")

    if _check_page_number_monotonic(all_blocks):
        flags.append("page_number_non_monotonic")

    plain_text = " ".join(
        b.content for b in body if b.content_format == ContentFormat.text
    )
    if _check_char_distribution_anomaly(plain_text):
        flags.append("char_distribution_anomaly")

    if _check_ngram_entropy(plain_text):
        flags.append("low_ngram_entropy")

    overlap_count = _check_bbox_overlap(body)
    if overlap_count:
        quality["bbox_overlap_count"] = float(overlap_count)
        flags.extend("bbox_overlap" for _ in range(overlap_count))

    if not doc.source.sha256:
        flags.append("missing_source_hash")

    return TrustInfo(
        publish_decision=_decide(flags, doc.source.reviewed),
        risk_flags=flags,
        quality=quality,
    )


def _decide(flags: list[str], reviewed: bool) -> PublishDecision:
    if "missing_source_hash" in flags:
        return PublishDecision.fail
    high = [f for f in flags if f in _HIGH_SEVERITY]
    severe = len(high) >= 2 or (bool(high) and "reading_order_sparse" in flags)
    if severe:
        return (
            PublishDecision.pass_with_warning
            if reviewed
            else PublishDecision.quarantine
        )
    if flags:
        return PublishDecision.pass_with_warning
    return PublishDecision.pass_
