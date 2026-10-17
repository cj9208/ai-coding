"""exam_builder 的 Web UI(设计 §7/D-8):FastAPI + Jinja + 原生 JS,无 auth。

三页:/docs(章节树)、/search(检索+勾选)、/compose(排序+生成)。
选择集放在浏览器 localStorage,服务端无状态;公式 MVP 不渲染,只显示徽标(D-3)。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from . import compose as compose_mod
from . import config
from .store import Document, Question, Store

PACKAGE_DIR = Path(__file__).parent
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_EXT_OK = {"png", "jpeg", "jpg", "wmf", "emf"}


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store()
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app = FastAPI(title="组卷", docs_url=None, redoc_url=None)
    app.state.store = store
    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static"
    )

    @app.get("/")
    def index():  # noqa: ANN202
        return RedirectResponse("/search")

    @app.get("/docs")
    def docs(request: Request):  # noqa: ANN202
        with store.client.session() as db:
            rows = (
                db.query(
                    Document.chapter,
                    Document.section,
                    Document.lesson,
                    Document.title,
                    Document.status,
                    func.count(Question.id),
                )
                .outerjoin(Question, Question.doc_id == Document.id)
                .group_by(
                    Document.chapter,
                    Document.section,
                    Document.lesson,
                    Document.title,
                    Document.status,
                )
                .order_by(Document.section, Document.lesson)
                .all()
            )
        return templates.TemplateResponse(request, "docs.html", {"rows": rows})

    @app.get("/search")
    def search(  # noqa: ANN202
        request: Request,
        q: str = "",
        chapter: str = "",
        section: str = "",
        lesson: str = "",
    ):
        with store.client.session() as db:
            hits = store.search(
                db, q=q, chapter=chapter, section=section, lesson=lesson
            )
            cards = []
            for hit in hits:
                doc = db.get(Document, hit.doc_id)
                manifest = store.manifest(hit)
                thumb = next((m for m in manifest if m["role"] == "figure"), None)
                cards.append(
                    {
                        "q": hit,
                        "doc": doc,
                        "thumb": thumb,
                        "lines": hit.stem_text.splitlines(),
                    }
                )
            chapters = [
                r[0]
                for r in db.query(Document.chapter)
                .filter(Document.chapter != "")
                .distinct()
                .order_by(Document.chapter)
                .all()
            ]
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "cards": cards,
                "q": q,
                "chapter": chapter,
                "section": section,
                "lesson": lesson,
                "chapters": chapters,
            },
        )

    @app.get("/media/{sha}.{ext}")
    def media(sha: str, ext: str):  # noqa: ANN202
        if not _SHA_RE.match(sha) or ext not in _EXT_OK:
            return RedirectResponse("/search")
        path = config.media_path(sha, ext)
        if not path.is_file():
            return RedirectResponse("/search")
        return FileResponse(path)

    @app.get("/compose")
    def compose_page(request: Request, ids: str = ""):  # noqa: ANN202
        qids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        items = []
        with store.client.session() as db:
            for i in qids:
                q = db.get(Question, i)
                if q is not None:
                    items.append({"q": q, "lines": q.stem_text.splitlines()})
        return templates.TemplateResponse(
            request, "compose.html", {"items": items, "ids": ids}
        )

    @app.post("/compose")
    def compose_post(ids: str = Form(""), title: str = Form("")):  # noqa: ANN202
        qids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        if not qids:
            return RedirectResponse("/search", status_code=303)
        stem = "-".join(map(str, qids))[:60]
        out = config.db_path().parent / "papers" / f"paper-{stem}.docx"
        compose_mod.compose(store, qids, title, out)
        with store.client.session() as db:
            from .store import Paper

            db.add(
                Paper(
                    title=title,
                    question_ids_json=",".join(map(str, qids)),
                    out_path=str(out),
                )
            )
            db.commit()
        return FileResponse(
            out,
            filename=out.name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    return app
