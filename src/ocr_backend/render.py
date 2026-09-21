"""Projections from the contract to what consumers actually want to read.

Why this module exists: the contract keeps every block a backend produced, but
consumers need *views* of it — plain text for a summarizer prompt, markdown for
a human or an LLM. Those views are plain functions over ``OcrDocument``, never a
reason to store a second copy of the document and never the backend's own
markdown output (Paddle's ``markdown_ignore_labels`` silently drops seven block
types; we keep everything and decide at projection time instead).

Two projections ship in v1:

- :func:`page_text` — reading order, body text only (page furniture skipped).
- :func:`document_markdown` — all kept blocks, rendered as markdown.

Both are intentionally dumb: no layout heuristics, no column reflow, no
image -> placeholder conversion. If a consumer needs more, it composes on top.
"""

from __future__ import annotations

from .contract import BlockKind, ContentFormat, OcrBlock, OcrDocument, OcrPage

PAGE_FURNITURE: frozenset[BlockKind] = frozenset(
    {BlockKind.header, BlockKind.footer, BlockKind.page_number}
)
"""Blocks that belong to the page, not to the document body. Skipped by both
projections by default; callers may pass their own ``skip`` set instead."""


def _reading_order(blocks: list[OcrBlock]) -> list[OcrBlock]:
    """Blocks sorted by backend reading order, unordered ones last.

    ``order=None`` means the backend deliberately excluded the block from the
    reading flow (caption / figure / furniture); for those, keep the backend's
    list position (``id``) as a stable tiebreaker.
    """
    return sorted(
        blocks,
        key=lambda b: (b.order is None, b.order if b.order is not None else b.id),
    )


def page_text(page: OcrPage, *, skip: frozenset[BlockKind] = PAGE_FURNITURE) -> str:
    """Page body as plain text: blocks in reading order, paragraphs split by
    a blank line, furniture skipped.

    Content is used verbatim regardless of ``content_format`` (a table's HTML
    and a formula's LaTeX read poorly as text on purpose — select only the
    kinds you want to keep via ``skip`` if that matters to you).
    """
    kept = [
        b.content.strip()
        for b in _reading_order(page.blocks)
        if b.kind not in skip and b.content.strip()
    ]
    return "\n\n".join(kept)


def document_markdown(
    doc: OcrDocument, *, skip: frozenset[BlockKind] = PAGE_FURNITURE
) -> str:
    """Whole document as markdown, page after page.

    Rendering rules (v1): titles become ATX headings — ``doc_title`` -> ``#``,
    any other title -> ``##``, graded on ``raw_label`` because the kind alone
    does not carry the level; every other block's ``content`` is emitted as-is
    (paragraph text, table HTML, formula LaTeX). Pages are separated by a blank
    line; blocks that project to nothing (e.g. a figure with empty content) are
    skipped.
    """
    parts: list[str] = []
    for page in doc.pages:
        for block in _reading_order(page.blocks):
            if block.kind in skip:
                continue
            text = _block_markdown(block)
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _block_markdown(block: OcrBlock) -> str:
    content = block.content.strip()
    if not content:
        return ""
    if block.kind is BlockKind.title:
        level = 1 if block.raw_label == "doc_title" else 2
        return f"{'#' * level} {content}"
    if block.content_format is ContentFormat.latex:
        return _wrap_latex(content)
    return content


def _wrap_latex(content: str) -> str:
    """Give bare LaTeX math delimiters so markdown renderers show it as math.

    Only adds ``$$...$$`` when the adapter did not already normalize a wrapped
    form in — this function must stay idempotent over its own output.
    """
    if content.startswith("$"):
        return content
    return f"$$\n{content}\n$$"
