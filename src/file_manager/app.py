from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from storage import SqliteClient

from . import models  # noqa: F401 - 注册 ORM 模型
from .config import Settings, load_settings
from .database import Base, get_db
from .fts import FILES_INDEX
from .routers import api, pages

PACKAGE_DIR = Path(__file__).parent
CN_TZ = timezone(timedelta(hours=8))  # 固定 +8，不依赖系统时区数据库


def format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite 读回的是 naive，按 UTC 还原
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)

    storage = SqliteClient(settings.db_url)
    storage.init_schema(Base.metadata)
    with storage.session() as db:
        FILES_INDEX.create(db)
        db.commit()

    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["dt"] = format_dt

    app = FastAPI(title="文件管理器")
    app.state.settings = settings
    app.state.storage = storage
    app.state.templates = templates

    app.mount(
        "/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static"
    )
    app.include_router(api.router)
    app.include_router(pages.router)

    app.dependency_overrides[get_db] = _db_override(storage)

    return app


def _db_override(storage):
    def override():
        with storage.session() as db:
            yield db

    return override
