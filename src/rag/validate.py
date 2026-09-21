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

Severity rule: two high-severity flags (or a metadata failure) quarantine;
one warns; clean passes. Thresholds are calibration inputs, not constants.
"""

from __future__ import annotations

from ocr_backend.contract import BlockKind, OcrBlock

from .contract import CanonicalDoc, PublishDecision, TrustInfo
from .structure import FURNITURE_KINDS, reading_order_blocks

_MIN_TEXT_PER_PAGE = 30.0
_MIN_ORDERED_RATIO = 0.5
_HIGH_SEVERITY = frozenset({"low_text_density", "table_unstructured"})
_TABLE_MARKERS = ("<table", "<tr", "|")


def assess(doc: CanonicalDoc) -> TrustInfo:
    """Run the proxy checks and return the gate decision; never mutates the
    document."""
    flags: list[str] = []
    quality: dict[str, float] = {}

    pairs = reading_order_blocks(doc.document)
    body: list[OcrBlock] = [b for _, b in pairs if b.kind not in FURNITURE_KINDS]

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
        # one flag per failed table: the gate counts signals, so two broken
        # tables must weigh more than one
        flags.extend("table_unstructured" for _ in range(len(tables) - structured))

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
