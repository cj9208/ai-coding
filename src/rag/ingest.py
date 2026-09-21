"""OcrBundleAcquirer: *.ocr.json (+ review.json sidecar) -> CanonicalDoc.

The bundle directory produced by ``ocr-backend parse`` is our acquisition
surface today; the protocol exists so a future corpus (markdown repo, mail
archive, web pages) plugs in beside it without any downstream change.

If a human-proofread ``review.json`` sits beside the machine JSON, it is
folded in with ``ocr_review.patch.apply_review`` and the document is marked
``reviewed`` — the machine JSON on disk is never mutated, same sidecar rule
the review UI lives by. Validation decisions come from :mod:`rag.validate`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ocr_backend.contract import OcrDocument
from ocr_review.patch import apply_review
from ocr_review.review import ReviewDocument

from .contract import CanonicalDoc, SourceInfo
from .structure import rebuild_section_paths
from .validate import assess


def load_ocr_document(path: Path) -> OcrDocument:
    return OcrDocument.model_validate_json(path.read_text(encoding="utf-8"))


class OcrBundleAcquirer:
    """Implements :class:`rag.protocols.Acquirer`."""

    name = "ocr_bundle"

    def fetch(self, bundle: Path) -> CanonicalDoc:
        """``bundle`` is the ``*.ocr.json`` file itself."""
        doc = load_ocr_document(bundle)
        reviewed = False
        sidecar = bundle.with_name(bundle.name.replace(".ocr.json", ".review.json"))
        if sidecar.is_file():
            review = ReviewDocument.model_validate(
                json.loads(sidecar.read_text(encoding="utf-8"))
            )
            doc = apply_review(doc, review)
            reviewed = True

        canonical = CanonicalDoc(
            doc_id=CanonicalDoc.make_doc_id(doc.source.sha256),
            source=SourceInfo(
                kind=doc.source.kind,
                path=str(doc.source.path),
                sha256=doc.source.sha256,
                extractor=f"{doc.backend.name}/{doc.backend.model}",
                page_count=doc.source.page_count,
                reviewed=reviewed,
                created_at=doc.created_at,
            ),
            document=doc,
            section_paths=rebuild_section_paths(doc),
        )
        canonical.trust = assess(canonical)
        return canonical
