from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_market_radar.models import Item


class KnowledgeBase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                source_key TEXT NOT NULL,
                entity TEXT NOT NULL,
                signal TEXT NOT NULL,
                tag TEXT NOT NULL DEFAULT 'feature',
                title TEXT NOT NULL,
                url TEXT,
                published_at TEXT,
                excerpt TEXT,
                fetched_at TEXT NOT NULL,
                raw_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_entity ON items(entity);
            CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
            CREATE TABLE IF NOT EXISTS source_state (
                source_key TEXT PRIMARY KEY,
                last_ok_at TEXT,
                last_error TEXT,
                last_items INTEGER
            );
            """)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(items)")}
        if "tag" not in columns:
            self._conn.execute(
                "ALTER TABLE items ADD COLUMN tag TEXT NOT NULL DEFAULT 'feature'"
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert(self, item: Item) -> bool:
        """Insert a new item; returns True only when it was newly added."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO items "
            "(fingerprint, source_key, entity, signal, tag, title, url, published_at, "
            "excerpt, fetched_at, raw_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.fingerprint,
                item.source_key,
                item.entity,
                item.signal,
                item.tag,
                item.title,
                item.url,
                item.published_at,
                item.excerpt,
                item.fetched_at,
                item.raw_hash,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def known_url_fingerprints(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT fingerprint FROM items WHERE fingerprint LIKE 'url:%'"
        ).fetchall()
        return {row[0] for row in rows}

    def set_source_ok(self, source_key: str, items: int, new_items: int) -> None:
        self._conn.execute(
            "INSERT INTO source_state (source_key, last_ok_at, last_items, last_error) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(source_key) DO UPDATE SET last_ok_at=excluded.last_ok_at, "
            "last_items=excluded.last_items, last_error=NULL",
            (source_key, _now(), items),
        )
        self._conn.commit()

    def set_source_error(self, source_key: str, error: str) -> None:
        self._conn.execute(
            "INSERT INTO source_state (source_key, last_error) VALUES (?, ?) "
            "ON CONFLICT(source_key) DO UPDATE SET last_error=excluded.last_error",
            (source_key, error[:500]),
        )
        self._conn.commit()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
