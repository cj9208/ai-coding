"""The review overlay model — what a human says about one machine OCR run.

Why an overlay instead of editing ``OcrDocument`` in place: the same corrected
document is worth two different things (exploration doc §2). For production
consumers we want *the fixed text*; for evaluating future OCR backends we want
the *pair* (machine output, human ground truth). Mutating the contract JSON
satisfies the first and destroys the second. So ``ocr-backend`` output stays
untouched and everything a human decides lands here, keyed by
``(page_index, block_id)`` and pinned to the exact ``OcrDocument`` it was
written against via ``ocr_json_sha256``.

Sparse on purpose: only blocks a human actually touched get an entry. That is
what makes :func:`ocr_review.patch.reanchor` possible later — when the same
PDF is re-OCRed by a newer backend, block ids may shift, and a sparse overlay
of "these few blocks were wrong" is both cheap to re-match and honest about
what was never checked.

JSON-native again (same rule as ``ocr_backend.contract``): plain types, ISO
strings for time, nothing that can't round-trip through ``model_dump_json``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field

from ocr_backend.contract import BlockKind, ContentFormat

REVIEW_SCHEMA_VERSION = "1.0"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class BlockState(StrEnum):
    """The human's verdict on one machine-produced block.

    ``kept`` and ``corrected`` both keep the block; the difference is whether
    the human edited it (``corrected`` means at least one patch field below is
    set). ``rejected`` marks a false detection — text that isn't really there,
    or a block split that shouldn't exist. ``added`` carries a block the
    backend missed entirely.
    """

    kept = "kept"
    corrected = "corrected"
    rejected = "rejected"
    added = "added"


class ReviewEntry(BaseModel):
    """One human judgement, applied over the matching contract block.

    Fields left ``None`` mean "inherit the machine value" — they are not a
    reset. So a ``corrected`` entry that only sets ``content`` keeps the
    backend's bbox/kind/order verbatim, and the overlay stays diffable against
    a re-run of the same model.
    """

    state: BlockState
    content: str | None = None
    kind: BlockKind | None = None
    content_format: ContentFormat | None = None
    bbox: tuple[float, float, float, float] | None = None
    order: int | None = None
    note: str | None = None
    """Why the human changed it — the one field a reviewer should feel free to
    fill; it is what makes a corpus of corrections readable months later."""

    updated_by: str | None = None
    updated_at: str | None = None


class PageReview(BaseModel):
    page_index: int
    status: str = "pending"
    """``pending`` | ``done`` — page-level progress, the reviewer's only
    workflow state."""

    entries: dict[int, ReviewEntry] = Field(default_factory=dict)
    """block_id -> judgement. Sparse: untouched blocks are absent."""


class ReviewTarget(BaseModel):
    """What this overlay was written against. Mismatch = stale review."""

    ocr_json_path: str
    ocr_json_sha256: str
    """Hash of the contract JSON file itself — the strongest cheap anchor for
    ``reanchor`` and for refusing to apply a review to a different run."""

    source_sha256: str
    """Hash of the PDF/image, equal to ``OcrSource.sha256``."""

    model: str
    pipeline_version: str


class ReviewDocument(BaseModel):
    """The whole human layer for one OCR run — what ``review.json`` is."""

    schema_version: str = REVIEW_SCHEMA_VERSION
    target: ReviewTarget
    created_at: str
    updated_at: str
    pages: list[PageReview] = Field(default_factory=list)

    def page(self, page_index: int) -> PageReview:
        """The page's review, created empty on first touch (and registered)."""
        for pr in self.pages:
            if pr.page_index == page_index:
                return pr
        pr = PageReview(page_index=page_index)
        self.pages.append(pr)
        return pr

    def entry(self, page_index: int, block_id: int) -> ReviewEntry | None:
        for pr in self.pages:
            if pr.page_index == page_index:
                return pr.entries.get(block_id)
        return None
