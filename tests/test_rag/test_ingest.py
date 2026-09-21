"""Acquisition: bundle on disk -> CanonicalDoc, review sidecar folded."""

from __future__ import annotations

import json

from ocr_review import review as rv
from rag.contract import PublishDecision


def test_plain_bundle_fetch(acquirer, inbox):
    doc = acquirer.fetch(inbox / "leave.ocr.json")
    assert doc.source.reviewed is False
    assert doc.trust.publish_decision == PublishDecision.pass_
    assert doc.section_paths["0:3"].endswith("1.1 审批流程")


def test_review_sidecar_folded_in(acquirer, inbox):
    bundle = inbox / "leave.ocr.json"
    review = rv.ReviewDocument(
        target=rv.ReviewTarget(
            ocr_json_path=str(bundle),
            ocr_json_sha256="a" * 64,
            source_sha256="b" * 64,
            model="m",
            pipeline_version="v",
        ),
        created_at="2026-09-21T00:00:00+08:00",
        updated_at="2026-09-21T00:00:00+08:00",
        pages=[
            rv.PageReview(
                page_index=0,
                entries={
                    5: rv.ReviewEntry(state=rv.BlockState.rejected),
                    3: rv.ReviewEntry(
                        state=rv.BlockState.corrected,
                        content="年假超过三天需要总监审批。",
                    ),
                },
            )
        ],
    )
    sidecar = inbox / "leave.review.json"
    sidecar.write_text(json.dumps(review.model_dump(mode="json")), encoding="utf-8")

    doc = acquirer.fetch(bundle)
    assert doc.source.reviewed is True
    texts = [b.content for p in doc.document.pages for b in p.blocks]
    assert "年假超过三天需要总监审批。" in texts  # corrected
    assert "公司内部制度" not in texts  # rejected header block gone
    # the machine bundle on disk was never mutated
    from ocr_backend.contract import OcrDocument

    machine = OcrDocument.model_validate_json(bundle.read_text(encoding="utf-8"))
    assert "年假超过三天需要部门经理审批。" in [
        b.content for p in machine.pages for b in p.blocks
    ]
