from __future__ import annotations

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(db_url: str) -> Engine:
    kwargs: dict = {}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}

        @event.listens_for(Engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001, ANN202
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return create_engine(db_url, **kwargs)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request):
    """FastAPI 依赖：每个请求一个会话，结束时关闭。"""
    factory = session_factory(request.app.state.engine)
    with factory() as db:
        yield db
