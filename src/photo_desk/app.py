"""FastAPI 应用装配。无账号（D-4）：进入即查看模式，编辑模式 M1/M2 再开。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from storage import SqliteClient

from .config import Settings
from .library import Base

_WEB_DIR = Path(__file__).parent / "web"


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_writable_dirs()
    storage = SqliteClient(settings.db_path)
    storage.init_schema(Base.metadata)

    app = FastAPI(title="photo_desk", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage = storage

    from .web.pages import router

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")
    return app
