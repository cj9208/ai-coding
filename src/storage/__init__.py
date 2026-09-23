"""Repo-wide shared storage layer — generic, per-database-type clients.

Why this package exists: every subproject that needs persistence re-implements
the same access knowledge (engine + WAL/foreign-keys PRAGMA + session factory
+ FTS5-with-CJK-folding + dedup hashing), and this machine has verified quirks
(FTS5 unicode61 drops CJK; trigram/editdist3 unavailable) that nobody should
rediscover. That knowledge belongs to the *database type*, not to any feature,
so it lives here: one module per type. Currently only SQLite is in use, so
``storage.sqlite`` is the only client — a future Postgres etc. would arrive as
a sibling module, not as a class hierarchy.

What deliberately stays OUT of this package: table definitions, ORM models,
and domain stores (e.g. research_agent's SessionStore) remain with their
projects. This layer hands each project a correctly-configured connection;
what you store is your business.

Usage::

    from storage import SqliteClient, FtsTable, fold_cjk

    client = SqliteClient("sqlite:///data/app/app.db")   # or Path(...)
    client.init_schema(MyBase.metadata)
    with client.session() as db:
        ...

Environment convention: none here. Callers pass ``db_url`` in from their own
Settings (``FM_DATABASE_URL`` etc. stay per project), mirroring how
subprojects feed ``llm_client.LLMSettings`` from their own config.
"""

from .cache import SqliteCache
from .fts import FtsTable, fold_cjk, match_expr, token_expr
from .sqlite import (
    BUSY_TIMEOUT_MS,
    SchemaTooNewError,
    SqliteClient,
    ensure_columns,
    get_user_version,
    make_engine,
    migrate,
    session_factory,
    set_user_version,
    sha256_hex,
    to_db_url,
)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "FtsTable",
    "SchemaTooNewError",
    "SqliteCache",
    "SqliteClient",
    "ensure_columns",
    "fold_cjk",
    "get_user_version",
    "make_engine",
    "match_expr",
    "migrate",
    "session_factory",
    "set_user_version",
    "sha256_hex",
    "to_db_url",
    "token_expr",
]
