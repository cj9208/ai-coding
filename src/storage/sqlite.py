"""SQLite client — the access layer for the only database type in use today.

Everything here is *SQLite knowledge*, learned the hard way in this repo:
- WAL + foreign_keys on every connection; the PRAGMA listener must attach to
  the engine *instance* (a class-level listener on ``Engine`` re-registers on
  every make_engine call and leaks into unrelated engines in the process).
- ``check_same_thread=False`` so one engine can serve FastAPI's threadpool.
- Lightweight additive schema patches (``ensure_columns``) instead of a
  migration framework: small single-user DBs only ever need ADD COLUMN.

Both consumption styles used in this repo are supported side by side:
SQLAlchemy (file_manager / research_agent) through ``session()``, and raw
DBAPI (ai_market_radar today) through ``connect()``.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Executable


def to_db_url(target: str | Path) -> str:
    """Accept both conventions seen in the repo: a full SQLAlchemy URL
    (``sqlite:///...``, what Settings carry) or a plain filesystem path to the
    .db file (what CLI ``--data-dir`` code builds)."""
    text_target = str(target)
    if text_target.startswith(("sqlite:", "postgres:", "mysql:")):
        return text_target
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def make_engine(db_url: str) -> Engine:
    kwargs: dict = {}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(db_url, **kwargs)

    if db_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001, ANN202
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def ensure_columns(
    engine: Engine, table: str, additions: Mapping[str, str]
) -> list[str]:
    """Add missing columns to an existing table (``{name: DDL fragment}``);
    returns the columns actually added. The scripted replacement for the
    PRAGMA table_info + ALTER TABLE patches ai_market_radar writes by hand."""
    existing = {c["name"] for c in inspect(engine).get_columns(table)}
    added: list[str] = []
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                added.append(name)
    return added


def sha256_hex(data: str | bytes, *, length: int | None = None) -> str:
    """Content digest for dedup. Projects choose their own truncation (full
    64 hex for file bytes, 16 for capture text); only the recipe is shared."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    digest = hashlib.sha256(raw).hexdigest()
    return digest[:length] if length else digest


class SqliteClient:
    """One instance per SQLite database file: owns the engine, hands out
    sessions and raw connections, and applies schema on demand."""

    def __init__(self, db: str | Path):
        self.db_url = to_db_url(db)
        self.engine: Engine = make_engine(self.db_url)
        self._factory: sessionmaker[Session] = session_factory(self.engine)

    # -- connections -----------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Session]:
        """ORM session; caller commits (matches every store style in-repo)."""
        with self._factory() as db:
            yield db

    def connect(self):  # noqa: ANN201 - DBAPI sqlite3.Connection
        """Raw DBAPI connection, WAL already applied via the engine's pool.
        For code paths that prefer plain SQL (ai_market_radar's style)."""
        return self.engine.raw_connection()

    # -- schema ------------------------------------------------------------------

    def init_schema(self, *metadata: MetaData) -> None:
        for md in metadata:
            md.create_all(self.engine)

    def ensure_columns(self, table: str, additions: Mapping[str, str]) -> list[str]:
        return ensure_columns(self.engine, table, additions)

    def dispose(self) -> None:
        self.engine.dispose()

    # -- escape hatches ------------------------------------------------------------

    def execute(
        self, statement: Executable, params: dict | None = None
    ):  # noqa: ANN201
        """One-shot execution outside any session (index DDL, PRAGMA reads)."""
        with self.engine.begin() as conn:
            return conn.execute(statement, params or {})
