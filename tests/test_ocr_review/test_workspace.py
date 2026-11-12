"""Inbox scan + workspace lifecycle (raster cache, atomic review, export)."""

import json

import pytest

from ocr_review.workspace import (
    StaleReviewError,
    Workspace,
    scan_inbox,
    validate_target,
)
from storage import sha256_hex


def test_scan_finds_bundle_and_resolves_bundled_source(bundle):
    items = scan_inbox(bundle["dir"])

    assert len(items) == 1
    item = items[0]
    assert item.stem == "scan"
    assert item.source == bundle["pdf"]  # found next to the JSON, not by path


def test_scan_skips_unparseable_and_missing_dir(bundle):
    (bundle["dir"] / "half.ocr.json").write_text("{not json", encoding="utf-8")

    items = scan_inbox(bundle["dir"])
    assert [i.stem for i in items] == ["scan"]
    assert scan_inbox(bundle["dir"] / "nope") == []


def test_open_renders_pages_at_contract_size(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)

    meta = json.loads(ws.meta_path.read_text(encoding="utf-8"))
    assert meta["stem"] == "scan"
    for i, page in enumerate(item.doc.pages):
        png = ws.pages_dir / f"page-{i:03d}.png"
        assert png.is_file()
        # 8-byte PNG signature: bytes 16-23 hold width/height as big-endian u32
        head = png.read_bytes()[16:24]
        w = int.from_bytes(head[:4], "big")
        h = int.from_bytes(head[4:], "big")
        assert (w, h) == (page.width, page.height), "raster must match contract grid"


def test_review_created_then_saved_and_reloaded(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)

    assert ws.load_review(item) is None
    review = ws.new_review(item)
    assert len(review.target.ocr_json_sha256) == 64
    review.page(0).status = "done"
    ws.save_review(item, review)

    again = Workspace(tmp_path / "work" / item.ws_id, item.ws_id)
    loaded = again.load_review(item)
    assert loaded is not None and loaded.page(0).status == "done"
    assert not list((tmp_path / "work").glob("*/review.json.tmp"))


def test_export_writes_reviewed_contract_and_markdown(bundle, tmp_path):
    from ocr_backend.contract import OcrDocument
    from ocr_review import review as rv
    from ocr_review.patch import apply_review

    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)
    review = ws.new_review(item)
    review.page(0).entries[0] = rv.ReviewEntry(state="corrected", content="人工修正")
    ws.save_review(item, review)

    paths = ws.export(item, review)

    reviewed = OcrDocument.model_validate_json(paths[0].read_text(encoding="utf-8"))
    assert reviewed.pages[0].blocks[0].content == "人工修正"
    assert "人工修正" in paths[1].read_text(encoding="utf-8")
    # the machine artifact itself is untouched
    assert "人工修正" not in bundle["json"].read_text(encoding="utf-8")
    # and apply_review is idempotent over the machine doc
    assert apply_review(item.doc, review).model_dump() == reviewed.model_dump()


def test_old_in_memory_snapshot_cannot_receive_new_hash(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)
    original = ws.new_review(item)
    raw = bundle["json"].read_text(encoding="utf-8")
    bundle["json"].write_text(raw + "\n", encoding="utf-8")
    with pytest.raises(StaleReviewError):
        ws.new_review(item)
    with pytest.raises(StaleReviewError):
        ws.save_review(item, original)
    with pytest.raises(StaleReviewError):
        ws.export(item, original)
    assert not ws.review_path.exists()
    assert not ws.export_dir.exists()


def test_same_ocr_bytes_moved_to_new_path_keep_valid_review(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)
    review = ws.new_review(item)
    ws.save_review(item, review)
    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / bundle["json"].name).write_bytes(bundle["json"].read_bytes())
    moved_item = scan_inbox(copied)[0]
    assert moved_item.ocr_json != item.ocr_json
    loaded = ws.load_review(moved_item)
    assert loaded is not None
    assert loaded.target.ocr_json_sha256 == review.target.ocr_json_sha256


def test_open_stale_item_leaves_meta_and_pages_untouched(bundle, tmp_path):
    """_ensure must validate before any write: a stale open raises without
    rewriting meta.json or re-rendering the page cache."""
    item = scan_inbox(bundle["dir"])[0]
    work_root = tmp_path / "work"
    ws = Workspace.open(work_root, item)
    protected = [ws.meta_path, *sorted(ws.pages_dir.glob("page-*.png"))]
    assert ws.meta_path in protected and len(protected) == 1 + len(item.doc.pages)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected}

    # a re-OCR changes the file; `item` still carries the old hash
    changed = json.loads(bundle["json"].read_text(encoding="utf-8"))
    changed["pages"][0]["blocks"][0]["content"] = "NEW unrelated block"
    bundle["json"].write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(StaleReviewError):
        Workspace.open(work_root, item)
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected} == before


def test_progress_without_review_is_zero(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)

    assert ws.progress(item) == (0, len(item.doc.pages))


def test_progress_counts_saved_done_pages(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)
    review = ws.new_review(item)
    review.page(0).status = "done"
    ws.save_review(item, review)

    assert ws.progress(item) == (1, len(item.doc.pages))


def test_progress_rejects_stale_item(bundle, tmp_path):
    item = scan_inbox(bundle["dir"])[0]
    ws = Workspace.open(tmp_path / "work", item)
    raw = bundle["json"].read_text(encoding="utf-8")
    bundle["json"].write_text(raw + "\n", encoding="utf-8")

    with pytest.raises(StaleReviewError):
        ws.progress(item)


def test_validate_target_unreadable_file(bundle):
    item = scan_inbox(bundle["dir"])[0]
    bundle["json"].unlink()

    with pytest.raises(StaleReviewError, match="文件不可读"):
        validate_target(item, None)


def test_validate_target_in_memory_doc_mismatch(bundle):
    item = scan_inbox(bundle["dir"])[0]
    item.doc.pages[0].blocks[0].content = "edited only in memory"
    # the file still matches the item's hash — only the in-memory doc drifted
    assert sha256_hex(bundle["json"].read_bytes()) == item.ocr_json_sha256

    with pytest.raises(StaleReviewError, match="与内存文档不一致"):
        validate_target(item, None)
