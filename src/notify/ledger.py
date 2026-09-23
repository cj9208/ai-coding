"""The ledger: ``events`` + ``deliveries``, rows are never deleted (§4.3).

"Delivered" is a per-(event, channel) row advance in ``deliveries``, not a
boolean on ``events`` — partial success across channels is the normal state,
and the audit surface must be able to answer "did that alert actually go
out" (the move_ledger "operation history *is* the audit" posture).

Uses the shared access layer (``storage.sqlite``: WAL, busy_timeout) with
plain SQL — no ORM, per the ai_market_radar style.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from storage.sqlite import ensure_columns, make_engine, migrate, to_db_url

from . import config
from .events import Event, Severity

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    project   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    severity  TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'alert')),
    dedup_key TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project_kind_ts
    ON events (project, kind, ts);
CREATE TABLE IF NOT EXISTS deliveries (
    event_id INTEGER NOT NULL REFERENCES events (id),
    channel  TEXT NOT NULL,
    status   TEXT NOT NULL
        CHECK (status IN ('pending', 'sent', 'failed', 'suppressed')),
    detail   TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, channel)
);
"""

#: M0 ledgers predate the attempts counter. Adding a column is the first tier
#: of docs/storage-usage-guide.md §4's two — no rebuild, no rename, so
#: ensure_columns is enough and it stays idempotent across opens.
_ADDITIONS = {"deliveries": {"attempts": "INTEGER NOT NULL DEFAULT 0"}}

#: the stamp baseline is the second tier, adopted here because this change is
#: what touched the store (the guide's "whoever opens it wires it" rule).
#: v1 = the M1 shape: deliveries.attempts present.
SCHEMA_VERSION = 1
MIGRATIONS: dict[int, Callable[[Engine], None] | None] = {1: None}

_INSERT_EVENT = text(
    "INSERT INTO events (ts, project, kind, severity, dedup_key, payload)"
    " VALUES (:ts, :project, :kind, :severity, :dedup_key, :payload)"
)

# alert first, then warn, then info; FIFO within a severity (§4.4).
# The CASE ranks mirror events.SEVERITY_RANK — test_pending_is_alert_first
# in tests/test_notify/test_ledger.py is the guard against drift.
#
# The severity filter is what keeps info out of the immediate-delivery path
# (§4.4: info waits for the digest) without a second query shape; an info
# event carrying payload {"urgent": true} is the documented escape hatch.
_SELECT_PENDING = text("""
    SELECT e.id, e.ts, e.project, e.kind, e.severity, e.dedup_key, e.payload
    FROM events e
    WHERE NOT EXISTS (
        SELECT 1 FROM deliveries d
        WHERE d.event_id = e.id AND d.channel = :channel AND d.status = 'sent'
    )
    AND (e.severity IN :severities
         OR json_extract(e.payload, '$.urgent') = true)
    ORDER BY CASE e.severity WHEN 'alert' THEN 0 WHEN 'warn' THEN 1
                             WHEN 'info' THEN 2 ELSE 9 END, e.ts
    LIMIT :limit
    """).bindparams(bindparam("severities", expanding=True))

_LAST_SENT_BY_KEY = text("""
    SELECT e.dedup_key AS dedup_key, MAX(d.at) AS last_at
    FROM deliveries d JOIN events e ON e.id = d.event_id
    WHERE d.channel = :channel AND d.status = 'sent'
    GROUP BY e.dedup_key
    """)

_ATTEMPTS = text("""
    SELECT event_id, attempts FROM deliveries
    WHERE channel = :channel AND event_id IN :ids
    """).bindparams(bindparam("ids", expanding=True))

_SUMMARY_ALL = text("""
    SELECT project, kind, severity, COUNT(*) AS n, MAX(ts) AS last_ts
    FROM events
    GROUP BY project, kind, severity
    ORDER BY project, kind, severity
    """)

_SUMMARY_BY_PROJECT = text("""
    SELECT project, kind, severity, COUNT(*) AS n, MAX(ts) AS last_ts
    FROM events
    WHERE project = :project
    GROUP BY project, kind, severity
    ORDER BY project, kind, severity
    """)

_UPSERT_DELIVERY = text("""
    INSERT INTO deliveries (event_id, channel, status, detail, at, attempts)
    VALUES (:event_id, :channel, :status, :detail, :at,
            CASE WHEN :status = 'failed' THEN 1 ELSE 0 END)
    ON CONFLICT (event_id, channel)
    DO UPDATE SET status = excluded.status,
                  detail = excluded.detail,
                  at = excluded.at,
                  attempts = CASE excluded.status
                      WHEN 'failed' THEN deliveries.attempts + 1
                      WHEN 'sent' THEN 0
                      ELSE deliveries.attempts END
    """)


def _row_to_event(row: Mapping[str, Any]) -> Event:
    return Event(
        id=int(row["id"]),
        ts=str(row["ts"]),
        project=str(row["project"]),
        kind=str(row["kind"]),
        severity=Severity(str(row["severity"])),
        dedup_key=str(row["dedup_key"]),
        payload=json.loads(str(row["payload"])),
    )


class Ledger:
    """One instance per ledger file."""

    def __init__(self, db: str | Path):
        self._engine: Engine = make_engine(to_db_url(db))
        with self._engine.begin() as conn:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
        for table, additions in _ADDITIONS.items():
            ensure_columns(self._engine, table, additions)
        migrate(self._engine, SCHEMA_VERSION, MIGRATIONS)

    # -- write side -----------------------------------------------------

    def insert(self, event: Event) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(_INSERT_EVENT, self._params(event))
            return int(result.lastrowid or 0)

    def insert_many(self, events: Sequence[Event]) -> int:
        """One transaction for a batch — used by bulk producers and the
        10k-acceptance test; per-event semantics are identical to insert()."""
        if not events:
            return 0
        with self._engine.begin() as conn:
            conn.execute(_INSERT_EVENT, [self._params(e) for e in events])
        return len(events)

    @staticmethod
    def _params(event: Event) -> dict[str, Any]:
        return {
            "ts": event.ts,
            "project": event.project,
            "kind": event.kind,
            "severity": event.severity.value,
            "dedup_key": event.dedup_key,
            "payload": json.dumps(event.payload, ensure_ascii=False, default=str),
        }

    def mark_delivery(
        self, event_id: int, channel: str, status: str, detail: str = ""
    ) -> None:
        from datetime import datetime, timezone

        with self._engine.begin() as conn:
            conn.execute(
                _UPSERT_DELIVERY,
                {
                    "event_id": event_id,
                    "channel": channel,
                    "status": status,
                    "detail": detail[:500],
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )

    # -- read side --------------------------------------------------------

    def pending_for(
        self,
        channel: str,
        limit: int = 500,
        severities: Sequence[str] = ("alert", "warn"),
    ) -> list[Event]:
        """Events not yet ``sent`` to this channel, alert-first.

        ``info`` is deliberately outside the default set: it waits for the
        digest (§4.4). An info event with payload ``{"urgent": true}`` still
        comes through — see _SELECT_PENDING.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                _SELECT_PENDING,
                {"channel": channel, "limit": limit, "severities": list(severities)},
            )
            return [_row_to_event(dict(m)) for m in rows.mappings()]

    def last_sent_by_key(self, channel: str) -> dict[str, str]:
        """``dedup_key -> ts`` of the newest sent delivery — policy's throttle
        input (§4.4: the first of a key always goes out, repeats merge)."""
        with self._engine.connect() as conn:
            rows = conn.execute(_LAST_SENT_BY_KEY, {"channel": channel})
            return {str(r["dedup_key"]): str(r["last_at"]) for r in rows.mappings()}

    def attempts(self, channel: str, event_ids: Sequence[int]) -> dict[int, int]:
        """Failed-attempt counts per event, for the escalation rule (§4.4)."""
        if not event_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(_ATTEMPTS, {"channel": channel, "ids": list(event_ids)})
            return {int(r["event_id"]): int(r["attempts"]) for r in rows.mappings()}

    def last_seen(self) -> dict[tuple[str, str], str]:
        """``(project, kind) -> newest ts`` across all severities — what
        :mod:`notify.rules` measures silence against (§4.5)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT project, kind, MAX(ts) AS last_ts FROM events"
                    " GROUP BY project, kind"
                )
            )
            return {
                (str(r["project"]), str(r["kind"])): str(r["last_ts"])
                for r in rows.mappings()
            }

    def last_event_ts_by_key(self) -> dict[str, str]:
        """``dedup_key -> newest ts`` of *emitted* events (not deliveries).
        The other half of rule idempotence: a silence alert already on the
        ledger inside the throttle window is not emitted again (§4.5)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT dedup_key, MAX(ts) AS last_ts FROM events GROUP BY dedup_key"
                )
            )
            return {str(r["dedup_key"]): str(r["last_ts"]) for r in rows.mappings()}

    def summary(
        self, project: str | None = None
    ) -> list[tuple[str, str, str, int, str]]:
        """(project, kind, severity, count, last_ts) rows — the status view."""
        statement = _SUMMARY_BY_PROJECT if project else _SUMMARY_ALL
        params: dict[str, Any] = {"project": project} if project else {}
        with self._engine.connect() as conn:
            rows = conn.execute(statement, params)
            return [
                (
                    str(r["project"]),
                    str(r["kind"]),
                    str(r["severity"]),
                    int(r["n"]),
                    str(r["last_ts"]),
                )
                for r in rows.mappings()
            ]

    def recent(self, limit: int = 20) -> list[Event]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, ts, project, kind, severity, dedup_key, payload"
                    " FROM events ORDER BY id DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [_row_to_event(dict(m)) for m in rows.mappings()]

    def total(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM events")).scalar() or 0)

    def deliveries_for(self, event_id: int) -> list[tuple[str, str, str]]:
        """(channel, status, detail) rows for one event — the audit surface."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT channel, status, detail FROM deliveries"
                    " WHERE event_id = :event_id ORDER BY channel"
                ),
                {"event_id": event_id},
            )
            return [
                (str(r["channel"]), str(r["status"]), str(r["detail"]))
                for r in rows.mappings()
            ]

    def dispose(self) -> None:
        self._engine.dispose()


@lru_cache(maxsize=8)
def _cached(db: str) -> Ledger:
    return Ledger(db)


def default() -> Ledger:
    """The ledger at the configured path (cached per path)."""
    return _cached(str(config.db_path()))


def open_ledger(db: str | Path) -> Ledger:
    """Explicit path — for tests and one-off tooling, no caching."""
    return Ledger(db)
