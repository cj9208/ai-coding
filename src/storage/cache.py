"""Persistent key-value cache backed by SQLite.

Why a dedicated class instead of a decorator: cache logic (hit/miss,
serialization, invalidation strategy) varies per call site, and wrapping it
in decorator magic hides those decisions. A plain class keeps every choice
visible at the call site while the storage knowledge (WAL PRAGMA, table
bootstrap, tag-based invalidation) lives in one place.

Usage::

    cache = SqliteCache("data/rag/cache.db")
    hit = cache.get(key, tag=str(corpus_version))
    if hit is None:
        result = expensive_work()
        cache.put(key, result_json, tag=str(corpus_version))
    else:
        result = json.loads(hit)
    cache.close()

Invalidation model: ``tag`` is an opaque string — callers choose the scheme
(corpus version, date, feature flag). ``get`` only returns entries whose tag
matches, so a version bump silently orphans old entries without any explicit
cleanup. ``clear(tag=...)`` deletes a specific generation when you want to
reclaim space.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class SqliteCache:
    """Disk-backed string key-value cache with tag-scoped reads.

    The cache stores ``str`` values — callers handle serialization (JSON,
    dataclass ``to_json``, etc.). This keeps the cache generic: it knows
    nothing about the domain objects flowing through it.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "  key TEXT PRIMARY KEY,"
            "  tag TEXT NOT NULL,"
            "  value TEXT NOT NULL,"
            "  created_at REAL NOT NULL DEFAULT (julianday('now'))"
            ")"
        )
        self._conn.commit()

    def get(self, key: str, tag: str) -> str | None:
        """Return the cached value if *key* exists with a matching *tag*,
        else ``None``. Tag mismatch = cache miss (old entries are silently
        skipped, not deleted — see ``clear``)."""
        row = self._conn.execute(
            "SELECT value FROM cache WHERE key = ? AND tag = ?", (key, tag)
        ).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str, tag: str) -> None:
        """Write or replace a cache entry."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, tag, value, created_at)"
            " VALUES (?, ?, ?, julianday('now'))",
            (key, tag, value),
        )
        self._conn.commit()

    def clear(self, tag: str | None = None) -> int:
        """Delete cache entries. With *tag*, only that generation; without,
        the entire cache. Returns the number of rows deleted."""
        if tag is not None:
            cur = self._conn.execute("DELETE FROM cache WHERE tag = ?", (tag,))
        else:
            cur = self._conn.execute("DELETE FROM cache")
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
