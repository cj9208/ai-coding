"""Applying a review overlay to a contract document (pure functions).

:func:`apply_review` produces a *new* ``OcrDocument`` — valid against the
unchanged contract, so downstream consumers (pdf_summarizer, file_manager)
read reviewed and un-reviewed documents through exactly the same code path.
Everything human lives in the overlay; nothing of that shows up here except as
corrected values.

:func:`reanchor` answers the harder question: a review written against model
A's output, re-applied to model B's output of the same pages. Block ids are
backend bookkeeping and do not survive a re-run, so matching is geometric +
textual: best bbox IoU on the same page, with a content-similarity floor.
Anything that fails to match is reported, never guessed at — silently
dropping or mis-placing a human correction is the one failure mode this must
not have.
"""

from __future__ import annotations

from dataclasses import dataclass

from ocr_backend.contract import OcrBlock, OcrDocument

from .review import BlockState, ReviewDocument, ReviewEntry

_HERITABLE_FIELDS = ("content", "kind", "content_format", "bbox", "order")
_HUMAN_RAW_LABEL = "human"


def apply_review(doc: OcrDocument, review: ReviewDocument) -> OcrDocument:
    """Overlay human judgements onto ``doc``; returns a new ``OcrDocument``."""
    overrides = {pr.page_index: pr.entries for pr in review.pages}
    pages = []
    for page in doc.pages:
        entries = dict(overrides.get(page.page_index, {}))
        machine_ids = {b.id for b in page.blocks}
        blocks: list[OcrBlock] = []
        for block in page.blocks:
            entry = entries.pop(block.id, None)
            if entry is None or entry.state is BlockState.kept:
                blocks.append(block)
            elif entry.state is BlockState.corrected:
                blocks.append(block.model_copy(update=_patch_fields(entry)))
            # rejected: the block simply does not survive
        for block_id, entry in entries.items():
            if entry.state is BlockState.added:
                if block_id in machine_ids:
                    raise ValueError(
                        f"page {page.page_index}: added id {block_id} clashes"
                    )
                blocks.append(_added_block(block_id, entry))
            elif entry.state is not BlockState.kept:
                raise ValueError(
                    f"page {page.page_index}: no machine block {block_id} "
                    f"for review state {entry.state!r}"
                )
            # a stray ``kept`` on an unknown id asserts nothing — ignore it
        pages.append(page.model_copy(update={"blocks": blocks}))
    return doc.model_copy(update={"pages": pages})


def _patch_fields(entry: ReviewEntry) -> dict:
    return {
        f: getattr(entry, f) for f in _HERITABLE_FIELDS if getattr(entry, f) is not None
    }


def _added_block(block_id: int, entry: ReviewEntry) -> OcrBlock:
    missing = [
        f for f in ("kind", "content_format", "bbox") if getattr(entry, f) is None
    ]
    if missing:
        raise ValueError(f"added block {block_id} missing fields: {', '.join(missing)}")
    return OcrBlock(
        id=block_id,
        kind=entry.kind,  # type: ignore[arg-type]
        raw_label=_HUMAN_RAW_LABEL,
        content=entry.content or "",
        content_format=entry.content_format,  # type: ignore[arg-type]
        bbox=entry.bbox,  # type: ignore[arg-type]
        order=entry.order,
        score=None,
    )


def next_free_block_id(doc: OcrDocument) -> int:
    """An id safe to use for a human-added block on any page."""
    return max((b.id for p in doc.pages for b in p.blocks), default=-1) + 1


# --------------------------------------------------------------------- reanchor


def _iou(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a, area_b = (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def _text_ratio(x: str, y: str) -> float:
    """Cheap order-insensitive similarity: shared characters over the longer
    side. Enough as a *floor* to reject coincidental geometric matches; not a
    diff metric."""
    if not x and not y:
        return 1.0
    if not x or not y:
        return 0.0
    common = sum(min(x.count(c), y.count(c)) for c in set(x) & set(y))
    return common / max(len(x), len(y))


_IOU_FLOOR = 0.5
_TEXT_FLOOR = 0.3


@dataclass
class Reanchored:
    """Result of re-applying a review to a fresh machine run."""

    review: ReviewDocument
    """Overlay carrying only what matched (added blocks are carried verbatim —
    they never referenced a machine block)."""

    lost: list[tuple[int, int]]
    """``(page_index, block_id)`` pairs whose target block is gone; a human
    must re-check these. ``kept`` verdicts are *not* rescued: nobody asserted
    them per-block, so an unmatched kept is just an unchecked block."""


def reanchor(
    review: ReviewDocument, old_doc: OcrDocument, new_doc: OcrDocument
) -> Reanchored:
    """Match each reviewed-against machine block in ``old_doc`` to its closest
    counterpart in ``new_doc`` and rebuild the overlay for ``new_doc`` ids."""
    old_blocks = {(p.page_index, b.id): b for p in old_doc.pages for b in p.blocks}
    new_by_page = {p.page_index: list(p.blocks) for p in new_doc.pages}

    carried = ReviewDocument(
        schema_version=review.schema_version,
        target=review.target,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )
    lost: list[tuple[int, int]] = []
    for pr in review.pages:
        page = carried.page(pr.page_index)
        page.status = pr.status
        added = {bid for bid, e in pr.entries.items() if e.state is BlockState.added}
        for block_id in sorted(added):
            page.entries[block_id] = pr.entries[block_id]
        matched, unmatched = _match_page(
            [bid for bid, e in pr.entries.items() if bid not in added],
            old_blocks,
            pr.page_index,
            new_by_page.get(pr.page_index, []),
            occupied=added,
        )
        for new_id, old_id in matched.items():
            page.entries[new_id] = pr.entries[old_id]
        for old_id in unmatched:
            entry = pr.entries[old_id]
            if entry.state is not BlockState.kept:
                # a kept verdict on a vanished block asserts nothing to lose;
                # any real correction must never vanish quietly
                lost.append((pr.page_index, old_id))
    return Reanchored(review=carried, lost=lost)


def _match_page(
    old_ids: list[int],
    old_blocks: dict[tuple[int, int], OcrBlock],
    page_index: int,
    candidates: list[OcrBlock],
    occupied: set[int],
) -> tuple[dict[int, int], list[int]]:
    """Greedy one-to-one assignment: highest-scoring pair claims its block
    first, so two corrections can never land on one id and overwrite each
    other. Returns ``{new_id: old_id}`` and the old ids left unmatched."""
    scored: list[tuple[float, int, int]] = []
    for old_id in old_ids:
        old = old_blocks.get((page_index, old_id))
        if old is None:
            continue
        for block in candidates:
            iou = _iou(old.bbox, block.bbox)
            if iou < _IOU_FLOOR:
                continue
            ratio = _text_ratio(old.content, block.content)
            if ratio < _TEXT_FLOOR:
                continue
            scored.append((iou + ratio, old_id, block.id))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    taken_new, taken_old = set(occupied), set()
    matched: dict[int, int] = {}
    for _score, old_id, new_id in scored:
        if old_id in taken_old or new_id in taken_new:
            continue
        taken_old.add(old_id)
        taken_new.add(new_id)
        matched[new_id] = old_id
    return matched, [old_id for old_id in old_ids if old_id not in taken_old]
