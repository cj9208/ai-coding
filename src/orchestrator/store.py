"""SQLite persistence for the runtime — three tables (see design
"Persistence"): ``requests`` (envelope, authoritative root), ``runtime_objects``
(append-only projections of the other per-request objects), ``events`` (a
derived replay/monitoring projection). Snapshots in ``requests``/
``runtime_objects`` are the source of truth; events never are.

Uses the shared :class:`storage.SqliteClient` so WAL/foreign-key PRAGMAs are
configured once repo-wide. Objects are stored as their JSON projection with a
``kind`` + monotonic ``seq`` so a request's full decision path reconstructs in
order (``orchestrate replay``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from storage import SqliteClient

from .contracts import RequestEnvelope
from .ids import new_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    session_id   TEXT,
    user_id      TEXT,
    status       TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_objects (
    object_id    TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (request_id, kind, seq)
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload_json TEXT,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_objects_request ON runtime_objects (request_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_request ON events (request_id, id);
"""


class Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = str(db_path) if db_path else _default_db()
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        self._client = SqliteClient(resolved)
        with self._client.engine.begin() as conn:
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(text(stmt))

    # --- requests ------------------------------------------------------------
    def create_request(self, envelope: RequestEnvelope) -> None:
        now = envelope.timestamp_start_ms
        with self._client.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO requests (request_id, session_id, user_id, status,"
                    " envelope_json, created_at_ms, updated_at_ms)"
                    " VALUES (:rid, :sid, :uid, :status, :env, :c, :u)"
                ),
                {
                    "rid": envelope.request_id,
                    "sid": envelope.session_id,
                    "uid": envelope.user_id,
                    "status": envelope.state.current_status.value,
                    "env": envelope.model_dump_json(),
                    "c": now,
                    "u": now,
                },
            )

    def update_request(self, envelope: RequestEnvelope, now_ms: int) -> None:
        with self._client.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE requests SET status = :status, envelope_json = :env,"
                    " updated_at_ms = :u WHERE request_id = :rid"
                ),
                {
                    "status": envelope.state.current_status.value,
                    "env": envelope.model_dump_json(),
                    "u": now_ms,
                    "rid": envelope.request_id,
                },
            )

    def get_request(self, request_id: str) -> RequestEnvelope | None:
        with self._client.engine.connect() as conn:
            row = conn.execute(
                text("SELECT envelope_json FROM requests WHERE request_id = :rid"),
                {"rid": request_id},
            ).first()
        return RequestEnvelope.model_validate_json(row[0]) if row else None

    def list_requests(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._client.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT request_id, status, user_id, updated_at_ms FROM requests"
                    " ORDER BY updated_at_ms DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).all()
        return [
            {"request_id": r[0], "status": r[1], "user_id": r[2], "updated_at_ms": r[3]}
            for r in rows
        ]

    # --- runtime objects -----------------------------------------------------
    def append_object(
        self, request_id: str, kind: str, payload: Any, now_ms: int
    ) -> str:
        object_id = new_id(kind[:4])
        with self._client.engine.begin() as conn:
            seq = conn.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_objects"
                    " WHERE request_id = :rid"
                ),
                {"rid": request_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO runtime_objects (object_id, request_id, kind, seq,"
                    " payload_json, created_at_ms)"
                    " VALUES (:oid, :rid, :kind, :seq, :payload, :c)"
                ),
                {
                    "oid": object_id,
                    "rid": request_id,
                    "kind": kind,
                    "seq": int(seq),
                    "payload": _dump(payload),
                    "c": now_ms,
                },
            )
        return object_id

    def objects(self, request_id: str) -> list[dict[str, Any]]:
        with self._client.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT kind, seq, payload_json FROM runtime_objects"
                    " WHERE request_id = :rid ORDER BY seq ASC, object_id ASC"
                ),
                {"rid": request_id},
            ).all()
        return [{"kind": r[0], "seq": r[1], "payload": json.loads(r[2])} for r in rows]

    def objects_of_kind(self, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        """Cross-request lookup for one object kind (e.g. pending handoffs)."""
        with self._client.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT request_id, object_id, payload_json, created_at_ms"
                    " FROM runtime_objects WHERE kind = :kind"
                    " ORDER BY created_at_ms DESC LIMIT :limit"
                ),
                {"kind": kind, "limit": limit},
            ).all()
        return [
            {
                "request_id": r[0],
                "object_id": r[1],
                "payload": json.loads(r[2]),
                "created_at_ms": r[3],
            }
            for r in rows
        ]

    # --- events --------------------------------------------------------------
    def emit(self, request_id: str, event: str, now_ms: int, **payload: Any) -> None:
        with self._client.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO events (request_id, event, payload_json, created_at_ms)"
                    " VALUES (:rid, :event, :payload, :c)"
                ),
                {
                    "rid": request_id,
                    "event": event,
                    "payload": json.dumps(payload, default=str) if payload else None,
                    "c": now_ms,
                },
            )

    def events(self, request_id: str) -> list[dict[str, Any]]:
        with self._client.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT event, payload_json, created_at_ms FROM events"
                    " WHERE request_id = :rid ORDER BY id ASC"
                ),
                {"rid": request_id},
            ).all()
        return [
            {
                "event": r[0],
                "payload": json.loads(r[1]) if r[1] else {},
                "created_at_ms": r[2],
            }
            for r in rows
        ]

    def close(self) -> None:
        self._client.dispose()


def _dump(payload: Any) -> str:
    if hasattr(payload, "model_dump_json"):
        return payload.model_dump_json()
    return json.dumps(payload, default=str)


def _default_db() -> str:
    from .config import DB_PATH

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return str(DB_PATH)
