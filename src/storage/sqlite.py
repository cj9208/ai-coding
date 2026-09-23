"""SQLite client — the access layer for the only database type in use today.

Everything here is *SQLite knowledge*, learned the hard way in this repo:
- WAL + foreign_keys + an explicit ``busy_timeout`` on every connection; the
  PRAGMA listener must attach to the engine *instance* (a class-level listener
  on ``Engine`` re-registers on every make_engine call and leaks into
  unrelated engines in the process).
- ``check_same_thread=False`` so one engine can serve FastAPI's threadpool.
- Two-tier schema evolution, no migration framework: ``ensure_columns`` for
  everything that is only ADD COLUMN, and a ``user_version`` stamp with
  ``migrate()`` for the day a change is *not* additive (rename, semantics,
  index rebuild) — see the graduation rule in docs/storage-usage-guide.md.

Both consumption styles used in this repo are supported side by side:
SQLAlchemy (file_manager / research_agent) through ``session()``, and raw
DBAPI (ai_market_radar today) through ``connect()``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Executable

#: How long a writer waits for the SQLite write lock before ``database is
#: locked``. pysqlite already defaults to 5 s; naming it here makes the
#: policy explicit and tunable in one place. Measured by
#: ``scripts/orch_bench_run.py`` (docs/orchestrator/05a-data-plane.md):
#: 8 concurrent writers turn a 32 ms P99 into 2.2 s *inside* this window —
#: contention surfaces as tail latency first, errors only past the timeout.
#: Waiting is a band-aid; batching the writes (05a step 3) is the cure.
BUSY_TIMEOUT_MS = 5000


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
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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


class SchemaTooNewError(RuntimeError):
    """The DB carries a schema stamp newer than this checkout understands."""


def get_user_version(engine: Engine) -> int:
    """The schema stamp in the DB header (0 = never stamped)."""
    with engine.connect() as conn:
        return int(conn.execute(text("PRAGMA user_version")).scalar_one())


def set_user_version(engine: Engine, version: int) -> None:
    """Write the schema stamp; PRAGMA takes no bind parameters."""
    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA user_version={int(version)}"))


def migrate(
    engine: Engine,
    current: int,
    steps: Mapping[int, Callable[[Engine], None] | None] | None = None,
) -> int:
    """Bring the DB up to ``current``, the checkout's schema version; returns
    it. ``steps[v]`` transforms a v-1 DB into v — or ``None`` for a
    stamp-only bump, which is how a project adopts the convention: declare
    ``SCHEMA_VERSION = 1`` with ``MIGRATIONS = {1: None}`` and every existing
    versionless file gets baselined on next open. The stamp is written after
    each step, so an interrupted multi-step run resumes where it stopped.
    A stamp newer than ``current`` raises :class:`SchemaTooNewError` instead
    of letting old code corrupt new data."""
    version = get_user_version(engine)
    if version > current:
        raise SchemaTooNewError(
            f"database is at schema version {version}, this checkout only "
            f"knows up to {current} — update the code before opening it"
        )
    steps = steps or {}
    for target in range(version + 1, current + 1):
        step = steps.get(target)
        if step is not None:
            step(engine)
        set_user_version(engine, target)
    return current


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

    def migrate(
        self,
        current: int,
        steps: Mapping[int, Callable[[Engine], None] | None] | None = None,
    ) -> int:
        return migrate(self.engine, current, steps)

    def dispose(self) -> None:
        self.engine.dispose()

    # -- escape hatches ------------------------------------------------------------

    def execute(
        self, statement: Executable, params: dict | None = None
    ):  # noqa: ANN201
        """One-shot execution outside any session (index DDL, PRAGMA reads)."""
        with self.engine.begin() as conn:
            return conn.execute(statement, params or {})
