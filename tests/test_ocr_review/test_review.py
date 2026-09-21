"""Review overlay model: JSON round-trip through plain types, helpers."""

from ocr_review import review as rv


def _target() -> rv.ReviewTarget:
    return rv.ReviewTarget(
        ocr_json_path="out/scan.ocr.json",
        ocr_json_sha256="a" * 64,
        source_sha256="b" * 64,
        model="PaddleOCR-VL-1.6-0.9B",
        pipeline_version="v1.6",
    )


def test_roundtrip_keeps_int_keys_and_sparse_entries():
    doc = rv.ReviewDocument(target=_target(), created_at="t", updated_at="t")
    page = doc.page(0)
    page.entries[3] = rv.ReviewEntry(state="corrected", content="fixed")
    page.status = "done"

    loaded = rv.ReviewDocument.model_validate_json(doc.model_dump_json())

    assert loaded.page(0).status == "done"
    assert loaded.page(0).entries[3].content == "fixed"
    assert loaded.page(1) is not loaded.page(0)  # created on demand
    assert loaded.entry(0, 3) is not None
    assert loaded.entry(0, 99) is None


def test_untouched_pages_stay_absent():
    """Sparseness is the point: a saved review lists only touched pages."""
    doc = rv.ReviewDocument(target=_target(), created_at="t", updated_at="t")
    assert doc.pages == []
    assert '"pages": []' in doc.model_dump_json(indent=2) or "pages: []" in str(doc)
