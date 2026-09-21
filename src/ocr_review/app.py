"""FastAPI app for the review UI — file-backed, no DB, no auth.

Identity is a manual name in a cookie (same honest stance as
``file_manager.identity``): it attributes corrections in ``review.json``,
nothing more. The inbox scan runs per request — pending work is literally
"what's in ``data/ocr_backend/out/``", so there is no cache to invalidate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ocr_backend.contract import OcrDocument

from . import review as rv
from .workspace import InboxDoc, Workspace, scan_inbox

PACKAGE_DIR = Path(__file__).parent
USER_COOKIE = "ocr_review_user"

_WS_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PAGE_RE = re.compile(r"^page-\d{3}\.png$")


class ReviewPut(BaseModel):
    review: rv.ReviewDocument
    base_updated_at: str | None = None
    """Value the client loaded; mismatch (or absent while a review exists)
    means someone else saved in the meantime — reject with 409."""


def find_item(inbox: Path, work_root: Path, ws_id: str) -> InboxDoc | None:
    """Inbox first; then workspaces registered earlier (``ocr-review add``,
    or an inbox bundle that was since moved away)."""
    for item in scan_inbox(inbox):
        if item.ws_id == ws_id:
            return item
    meta_path = work_root / ws_id / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ocr_json = Path(meta["ocr_json"])
        source = Path(meta["source"]) if meta.get("source") else None
        if ocr_json.is_file():
            doc = OcrDocument.model_validate_json(ocr_json.read_text(encoding="utf-8"))
            return InboxDoc(
                ocr_json=ocr_json,
                doc=doc,
                source=source if source and source.is_file() else None,
            )
    return None


def create_app(inbox: Path, work_root: Path) -> FastAPI:
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app = FastAPI(title="OCR 校对台")
    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static"
    )

    def item_or_404(ws_id: str) -> InboxDoc:
        if not _WS_ID_RE.match(ws_id):
            raise HTTPException(404, "bad workspace id")
        item = find_item(inbox, work_root, ws_id)
        if item is None:
            raise HTTPException(404, f"未找到文档 {ws_id}")
        return item

    # ------------------------------------------------------------------ pages

    @app.get("/")
    async def index(request: Request) -> Response:
        rows = []
        for it in scan_inbox(inbox):
            ws = Workspace(work_root / it.ws_id, it.ws_id)
            done, total = (
                ws.progress(it.doc)
                if ws.meta_path.is_file()
                else (0, len(it.doc.pages))
            )
            review = ws.load_review() if ws.meta_path.is_file() else None
            rows.append(
                {
                    "ws_id": it.ws_id,
                    "stem": it.stem,
                    "pages": len(it.doc.pages),
                    "done": done,
                    "model": it.doc.backend.model,
                    "source_ok": it.source is not None,
                    "updated_at": review.updated_at if review else "",
                }
            )
        return templates.TemplateResponse(
            request, "index.html", {"rows": rows, "inbox": str(inbox)}
        )

    @app.get("/w/{ws_id}")
    async def review_page(request: Request, ws_id: str) -> Response:
        item = item_or_404(ws_id)
        return templates.TemplateResponse(
            request, "review.html", {"ws_id": ws_id, "stem": item.stem}
        )

    # ------------------------------------------------------------------- api

    @app.get("/api/w/{ws_id}/document")
    async def get_document(ws_id: str) -> dict:
        item = item_or_404(ws_id)
        ws = Workspace.open(work_root, item)
        review = ws.load_review() or ws.new_review(item)
        return {
            "document": json.loads(item.doc.model_dump_json()),
            "review": json.loads(review.model_dump_json()),
            "review_is_new": ws.load_review() is None,
            "source_ok": item.source is not None,
            "stem": item.stem,
        }

    @app.put("/api/w/{ws_id}/review")
    async def put_review(ws_id: str, payload: ReviewPut, request: Request) -> dict:
        item = item_or_404(ws_id)
        ws = Workspace.open(work_root, item)
        existing = ws.load_review()
        if existing is not None and payload.base_updated_at != existing.updated_at:
            raise HTTPException(409, "他人已保存，请刷新后重试")
        review = payload.review
        review.updated_at = rv.now_iso()
        name = _decode_user(request)
        if name:
            # the client clears updated_by on entries it just changed; only
            # those get (re)stamped here, so per-entry history survives saves
            for page in review.pages:
                for entry in page.entries.values():
                    if not entry.updated_by:
                        entry.updated_by, entry.updated_at = name, review.updated_at
        ws.save_review(review)
        return {"updated_at": review.updated_at}

    @app.post("/api/w/{ws_id}/export")
    async def export(ws_id: str) -> dict:
        item = item_or_404(ws_id)
        ws = Workspace.open(work_root, item)
        review = ws.load_review()
        if review is None:
            raise HTTPException(400, "尚无人工修改可导出")
        paths = ws.export(item.doc, review)
        return {"paths": [str(p) for p in paths]}

    @app.get("/w/{ws_id}/pages/{fname}")
    async def page_image(ws_id: str, fname: str) -> FileResponse:
        if not _WS_ID_RE.match(ws_id) or not _PAGE_RE.match(fname):
            raise HTTPException(404)
        path = work_root / ws_id / "pages" / fname
        if not path.is_file():
            # the raster is a cache — regenerate rather than 404 on a direct hit
            Workspace.open(work_root, item_or_404(ws_id))
        if not path.is_file():
            raise HTTPException(404, "页图不存在（源 PDF 可能缺失）")
        return FileResponse(path)

    @app.post("/api/who")
    async def set_who(response: Response, name: str) -> dict:
        response.set_cookie(
            USER_COOKIE, quote(name), max_age=365 * 86400, samesite="lax"
        )
        return {"ok": True}

    return app


def _decode_user(request: Request) -> str | None:
    raw = request.cookies.get(USER_COOKIE)
    return unquote(raw).strip() if raw else None
