"""Review workspaces: what's pending (inbox scan) and where work is stored.

The inbox *is* the queue — ``ocr-backend parse`` (here or in the Docker
runner) drops ``<stem>.ocr.json`` + a copy of the source file into
``data/ocr_backend/out/`` whenever the machine is free; a reviewer browsing
later sees exactly those bundles. No task table, no state duplication.

Opening a document creates a workspace under ``data/ocr_review/<ws-id>/``
(``ws-id`` = first 12 hex of the source sha256, so re-OCRed copies of the same
PDF share one workspace). A workspace holds the only editable truth
(``review.json``), a regenerable page-image cache, and exports.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ocr_backend.contract import OcrDocument
from ocr_backend.render import document_markdown
from storage import sha256_hex
from utils.paths import REPO_ROOT, data_dir

from . import render_pages
from . import review as rv

INBOX_DEFAULT = data_dir("ocr_backend") / "out"
WORK_ROOT_DEFAULT = data_dir("ocr_review")


@dataclass
class InboxDoc:
    """One parsed document found in the inbox, plus its resolved source file."""

    ocr_json: Path
    doc: OcrDocument
    source: Path | None
    stem: str = field(init=False)

    def __post_init__(self) -> None:
        self.stem = self.ocr_json.stem.removesuffix(".ocr")

    @property
    def ws_id(self) -> str:
        return self.doc.source.sha256[:12]


def _resolve_source(ocr_json: Path, doc: OcrDocument) -> Path | None:
    """Prefer the self-contained bundle (source copied next to the JSON by
    ``ocr-backend parse``); fall back to the recorded path for older runs."""
    bundle = ocr_json.parent / Path(doc.source.path).name
    if bundle.is_file():
        return bundle
    for candidate in (Path(doc.source.path), REPO_ROOT / doc.source.path):
        if candidate.is_file():
            return candidate
    return None


def scan_inbox(inbox: Path) -> list[InboxDoc]:
    """Every parseable ``*.ocr.json`` under ``inbox``, newest first."""
    items: list[InboxDoc] = []
    if not inbox.is_dir():
        return items
    for json_path in inbox.glob("*.ocr.json"):
        try:
            doc = OcrDocument.model_validate_json(json_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue  # a half-written bundle is not a pending review
        items.append(
            InboxDoc(
                ocr_json=json_path,
                doc=doc,
                source=_resolve_source(json_path, doc),
            )
        )
    items.sort(key=lambda i: i.ocr_json.stat().st_mtime, reverse=True)
    return items


class Workspace:
    """Directory-backed state for reviewing one document."""

    def __init__(self, root: Path, ws_id: str) -> None:
        self.root = root
        self.ws_id = ws_id
        self.meta_path = root / "meta.json"
        self.review_path = root / "review.json"
        self.pages_dir = root / "pages"
        self.export_dir = root / "export"

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def open(cls, work_root: Path, item: InboxDoc) -> "Workspace":
        ws = cls(work_root / item.ws_id, item.ws_id)
        ws._ensure(item)
        return ws

    def _ensure(self, item: InboxDoc) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(
            json.dumps(
                {
                    "ocr_json": str(item.ocr_json),
                    "source": str(item.source) if item.source else None,
                    "stem": item.stem,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if item.doc.source.kind == "pdf" and item.source is not None:
            render_pages.render_pages(item.doc, item.source, self.pages_dir)

    @property
    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- review

    def load_review(self) -> rv.ReviewDocument | None:
        if not self.review_path.is_file():
            return None
        return rv.ReviewDocument.model_validate_json(
            self.review_path.read_text(encoding="utf-8")
        )

    def new_review(self, item: InboxDoc) -> rv.ReviewDocument:
        return rv.ReviewDocument(
            target=rv.ReviewTarget(
                ocr_json_path=str(item.ocr_json),
                ocr_json_sha256=sha256_hex(item.ocr_json.read_bytes()),
                source_sha256=item.doc.source.sha256,
                model=item.doc.backend.model,
                pipeline_version=item.doc.backend.pipeline_version,
            ),
            created_at=rv.now_iso(),
            updated_at=rv.now_iso(),
        )

    def save_review(self, review: rv.ReviewDocument) -> None:
        """Atomic write: temp file in-dir + os.replace, so a crash mid-save
        can never truncate the only editable truth."""
        tmp = self.review_path.with_suffix(".json.tmp")
        tmp.write_text(review.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self.review_path)

    # -------------------------------------------------------------- progress

    def progress(self, doc: OcrDocument) -> tuple[int, int]:
        """(pages done, total pages)."""
        review = self.load_review()
        if review is None:
            return 0, len(doc.pages)
        done = sum(1 for p in review.pages if p.status == "done")
        return done, len(doc.pages)

    # ---------------------------------------------------------------- export

    def export(self, doc: OcrDocument, review: rv.ReviewDocument) -> list[Path]:
        """Materialize the reviewed document as a plain contract JSON + md."""
        from .patch import apply_review

        out = apply_review(doc, review)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(self.meta["ocr_json"]).stem.removesuffix(".ocr")
        json_path = self.export_dir / f"{stem}.reviewed.json"
        json_path.write_text(out.model_dump_json(indent=2), encoding="utf-8")
        md_path = self.export_dir / f"{stem}.reviewed.md"
        md_path.write_text(document_markdown(out), encoding="utf-8")
        return [json_path, md_path]
