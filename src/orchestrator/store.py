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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
    updated_at_ms INTEGER NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1
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
-- the two cross-request reads: each sorted a whole table per call before
-- these existed (measured: list_requests P50 188 ms at 10^5 requests)
CREATE INDEX IF NOT EXISTS ix_requests_updated ON requests (updated_at_ms);
CREATE INDEX IF NOT EXISTS ix_objects_kind ON runtime_objects (kind, created_at_ms);
-- the quota gate's day read — this user's requests in one day window,
-- envelopes summed via json_extract (05c). Day-grain, so the window scan
-- is tiny and the index keeps it that way at Case A row counts
CREATE INDEX IF NOT EXISTS ix_requests_user_created ON requests (user_id, created_at_ms);
"""

#: day bucket for the quota gate (05c): UTC calendar day — deliberate v1
#: simplicity; per-tenant local days belong with 05b's identity step.
DAY_MS = 86_400_000


class ConflictError(RuntimeError):
    """Compare-and-set lost: another writer advanced this request's envelope
    since it was read. Raised instead of silently overwriting the other
    side's counters and state (docs/orchestrator/04-scaling.md §2).

    Deliberately *not* absorbed into the state machine: turning a conflict
    into a decision-table row would amend the contract (DP-5/DP-7). The
    caller — CLI today, queue worker later — decides retry or handoff.
    """

    def __init__(self, request_id: str, expected_version: int) -> None:
        super().__init__(
            f"request {request_id} was written by someone else "
            f"(expected version {expected_version})"
        )
        self.request_id = request_id
        self.expected_version = expected_version


class Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = str(db_path) if db_path else _default_db()
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        self._client = SqliteClient(resolved)
        with self._client.engine.begin() as conn:
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(text(stmt))
        # dbs created before the version column existed get it additively;
        # DEFAULT 1 matches what an old envelope's JSON already believes
        self._client.ensure_columns(
            "requests", {"version": "INTEGER NOT NULL DEFAULT 1"}
        )
        #: connection of an open :meth:`transact`, if any
        self._outer: Any = None

    # -- transaction grouping --------------------------------------------------
    @contextmanager
    def transact(self) -> Iterator[None]:
        """Commit everything written inside the block as ONE transaction.

        One loop pass writes a transition + a few objects + a few events;
        as separate commits each one is a write-lock round trip (~20 per
        turn, measured by ``scripts/orch_bench_run.py``). Nested calls reuse
        the open connection rather than starting a second transaction.
        """
        if self._outer is not None:
            yield
            return
        conn = self._client.engine.connect()
        trans = conn.begin()
        self._outer = conn
        try:
            yield
            trans.commit()
        except BaseException:
            trans.rollback()
            raise
        finally:
            self._outer = None
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[Any]:
        """The one connection path every writer uses: the open
        :meth:`transact` connection when there is one, else its own commit."""
        if self._outer is not None:
            yield self._outer
            return
        with self._client.engine.begin() as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[Any]:
        """Reads join the open :meth:`transact` connection too: a loop pass
        that reads back objects (the handoff packet does) must see what it
        appended a moment earlier, before that write is committed."""
        if self._outer is not None:
            yield self._outer
            return
        with self._client.engine.connect() as conn:
            yield conn

    # --- requests ------------------------------------------------------------
    def create_request(self, envelope: RequestEnvelope) -> None:
        now = envelope.timestamp_start_ms
        with self._write() as conn:
            conn.execute(
                text(
                    "INSERT INTO requests (request_id, session_id, user_id, status,"
                    " envelope_json, created_at_ms, updated_at_ms, version)"
                    " VALUES (:rid, :sid, :uid, :status, :env, :c, :u, :v)"
                ),
                {
                    "rid": envelope.request_id,
                    "sid": envelope.session_id,
                    "uid": envelope.user_id,
                    "status": envelope.state.current_status.value,
                    "env": envelope.model_dump_json(),
                    "c": now,
                    "u": now,
                    "v": envelope.state.version,
                },
            )

    def update_request(self, envelope: RequestEnvelope, now_ms: int) -> None:
        """Compare-and-set on ``version``: the write lands only if nobody
        else advanced the row since this envelope was read."""
        expected = envelope.state.version
        envelope.state.version = expected + 1
        with self._write() as conn:
            rowcount = (
                conn.execute(
                    text(
                        "UPDATE requests SET status = :status, envelope_json = :env,"
                        " updated_at_ms = :u, version = :new"
                        " WHERE request_id = :rid AND version = :old"
                    ),
                    {
                        "status": envelope.state.current_status.value,
                        "env": envelope.model_dump_json(),
                        "u": now_ms,
                        "new": envelope.state.version,
                        "old": expected,
                        "rid": envelope.request_id,
                    },
                ).rowcount
                or 0
            )
        if rowcount == 0:
            envelope.state.version = expected  # leave the caller's copy honest
            raise ConflictError(envelope.request_id, expected)

    def get_request(self, request_id: str) -> RequestEnvelope | None:
        with self._read() as conn:
            row = conn.execute(
                text(
                    "SELECT envelope_json, version FROM requests"
                    " WHERE request_id = :rid"
                ),
                {"rid": request_id},
            ).first()
        if not row:
            return None
        envelope = RequestEnvelope.model_validate_json(row[0])
        envelope.state.version = int(row[1])  # the column is the authority
        return envelope

    def list_requests(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._read() as conn:
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

    def llm_calls_today(
        self, user_id: str, now_ms: int, *, exclude_request_id: str | None = None
    ) -> int:
        """Sum of ``attempt_counters.llm_calls`` over this user's requests
        created in the current UTC day (05c quota gate). The envelope is
        the accounting authority (DP-8), so the sum reads the JSON projection
        with json_extract — rows written before the field existed return
        NULL and simply do not add. ``exclude_request_id`` drops the
        in-flight request, whose own counter the caller carries on the
        live envelope — including both would double-count."""
        day_start = now_ms - (now_ms % DAY_MS)
        sql = (
            "SELECT COALESCE(SUM("
            " json_extract(envelope_json, '$.attempt_counters.llm_calls')), 0)"
            " FROM requests"
            " WHERE user_id = :uid AND created_at_ms >= :start"
            " AND created_at_ms < :end"
        )
        params: dict[str, Any] = {
            "uid": user_id,
            "start": day_start,
            "end": day_start + DAY_MS,
        }
        if exclude_request_id is not None:
            sql += " AND request_id != :rid"
            params["rid"] = exclude_request_id
        with self._read() as conn:
            return int(conn.execute(text(sql), params).scalar_one())

    # --- runtime objects -----------------------------------------------------
    def append_object(
        self,
        request_id: str,
        kind: str,
        payload: Any,
        now_ms: int,
        seq: int | None = None,
    ) -> str:
        """Append one object. ``seq`` comes from the envelope counter
        (``state.next_seq``, DP-8) whenever the caller has the envelope —
        reading ``MAX(seq)+1`` here is a read-then-write race. The fallback
        exists for callers outside a turn (scripts, tests)."""
        object_id = new_id(kind[:4])
        with self._write() as conn:
            if seq is None:
                seq = int(
                    conn.execute(
                        text(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_objects"
                            " WHERE request_id = :rid"
                        ),
                        {"rid": request_id},
                    ).scalar_one()
                )
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
        with self._read() as conn:
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
        with self._read() as conn:
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
        with self._write() as conn:
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
        with self._read() as conn:
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
