"""Inbox scan + workspace lifecycle (raster cache, atomic review, export)."""

import json

from ocr_review.workspace import Workspace, scan_inbox


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

    assert ws.load_review() is None
    review = ws.new_review(item)
    assert len(review.target.ocr_json_sha256) == 64
    review.page(0).status = "done"
    ws.save_review(review)

    again = Workspace(tmp_path / "work" / item.ws_id, item.ws_id)
    loaded = again.load_review()
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
    ws.save_review(review)

    paths = ws.export(item.doc, review)

    reviewed = OcrDocument.model_validate_json(paths[0].read_text(encoding="utf-8"))
    assert reviewed.pages[0].blocks[0].content == "人工修正"
    assert "人工修正" in paths[1].read_text(encoding="utf-8")
    # the machine artifact itself is untouched
    assert "人工修正" not in bundle["json"].read_text(encoding="utf-8")
    # and apply_review is idempotent over the machine doc
    assert apply_review(item.doc, review).model_dump() == reviewed.model_dump()
