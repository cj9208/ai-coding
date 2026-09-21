"""apply_review / reanchor — the pure patch semantics over the contract."""

import pytest

from ocr_backend.contract import BlockKind, ContentFormat
from ocr_review import review as rv
from ocr_review.patch import apply_review, next_free_block_id, reanchor

from .conftest import make_doc


def _review() -> rv.ReviewDocument:
    return rv.ReviewDocument(
        target=rv.ReviewTarget(
            ocr_json_path="x",
            ocr_json_sha256="a" * 64,
            source_sha256="b" * 64,
            model="m",
            pipeline_version="v1.6",
        ),
        created_at="t",
        updated_at="t",
    )


def test_corrected_inherits_unset_fields(doc_pair):
    doc, _ = doc_pair
    review = _review()
    review.page(0).entries[0] = rv.ReviewEntry(state="corrected", content="fixed")

    out = apply_review(doc, review)

    block = out.pages[0].blocks[0]
    assert block.content == "fixed"
    assert block.bbox == doc.pages[0].blocks[0].bbox  # inherited
    assert block.kind is BlockKind.paragraph


def test_rejected_drops_and_added_appends(doc_pair):
    doc, _ = doc_pair
    review = _review()
    page = review.page(0)
    page.entries[1] = rv.ReviewEntry(state="rejected")
    page.entries[7] = rv.ReviewEntry(
        state="added",
        kind=BlockKind.table,
        content_format=ContentFormat.html,
        content="<table></table>",
        bbox=(10.0, 10.0, 20.0, 20.0),
    )

    out = apply_review(doc, review)

    blocks = out.pages[0].blocks
    assert [b.id for b in blocks] == [0, 7]
    added = blocks[1]
    assert added.raw_label == "human" and added.order is None


def test_added_incomplete_and_unknown_id_raise(doc_pair):
    doc, _ = doc_pair
    review = _review()
    review.page(0).entries[5] = rv.ReviewEntry(state="added")

    with pytest.raises(ValueError, match="missing fields"):
        apply_review(doc, review)

    review2 = _review()
    review2.page(0).entries[99] = rv.ReviewEntry(
        state="corrected", content="no such block"
    )
    with pytest.raises(ValueError, match="no machine block 99"):
        apply_review(doc, review2)


def test_exported_document_still_validates_as_contract(doc_pair):
    doc, _ = doc_pair
    review = _review()
    review.page(0).entries[0] = rv.ReviewEntry(state="corrected", content="ok")

    out = apply_review(doc, review)

    assert out.model_dump_json()  # round-trips through the unchanged contract
    assert out.pages[1].blocks == doc.pages[1].blocks  # untouched page identical


def test_next_free_block_id(doc_pair):
    doc, _ = doc_pair
    assert next_free_block_id(doc) == 2


def test_reanchor_shifted_ids_keep_human_edits(doc_pair):
    doc, _ = doc_pair
    review = _review()
    review.page(0).entries[0] = rv.ReviewEntry(state="corrected", content="fixed")
    review.page(1).entries[1] = rv.ReviewEntry(state="rejected")

    # new run: ids on page 0 shifted (block 0 -> 5), page 1's block 1 vanished
    new = make_doc(doc_pair[1], {0: (300, 400), 1: (301, 401)})
    new.pages[0].blocks[0].id = 5
    new.pages[1].blocks = new.pages[1].blocks[:1]

    result = reanchor(review, doc, new)

    assert result.review.entry(0, 5) is not None, "correction follows the match"
    assert result.review.entry(0, 5).content == "fixed"
    assert (1, 1) in result.lost, "a rejected-against-a-vanished-block must surface"


def test_reanchor_contested_match_never_overwrites(doc_pair):
    doc, _ = doc_pair
    # two old blocks nearly identical, so both would claim the same new block
    old0 = doc.pages[0].blocks[0]
    old1 = doc.pages[0].blocks[1]
    old1.bbox = (45.0, 50.0, 235.0, 64.0)
    old1.content = old0.content
    review = _review()
    review.page(0).entries[0] = rv.ReviewEntry(state="corrected", content="fix-A")
    review.page(0).entries[1] = rv.ReviewEntry(state="corrected", content="fix-B")

    new = make_doc(doc_pair[1], {0: (300, 400), 1: (301, 401)})
    new.pages[0].blocks = [new.pages[0].blocks[0]]  # both collapse into id 0

    result = reanchor(review, doc, new)

    assert result.review.entry(0, 0).content == "fix-A", "better match wins"
    assert (0, 1) in result.lost, "the loser must surface, not overwrite silently"


def test_reanchor_added_id_wins_contested_slot(doc_pair):
    doc, _ = doc_pair
    review = _review()
    review.page(0).entries[5] = rv.ReviewEntry(
        state="added",
        kind=BlockKind.paragraph,
        content_format=ContentFormat.text,
        content="mine",
        bbox=(1.0, 1.0, 2.0, 2.0),
    )
    review.page(0).entries[0] = rv.ReviewEntry(state="corrected", content="fixed")

    new = make_doc(doc_pair[1], {0: (300, 400), 1: (301, 401)})
    new.pages[0].blocks[0].id = 5  # collides with the added entry's id

    result = reanchor(review, doc, new)

    assert result.review.entry(0, 5).state is rv.BlockState.added
    assert (0, 0) in result.lost
