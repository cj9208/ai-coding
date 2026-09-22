"""The five-field hourglass (§4.1) and the task-side facade (§4.2).

``emit`` is the *only* thing a task is allowed to call. It is synchronous,
pure-local, and never raises: if the ledger itself is broken the event is
dropped with one stderr line — the notification layer's failure must not
become the task's failure. A test in tests/test_notify guards that no
channel concept (telegram, token, url…) leaks into this signature.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ALERT = "alert"

    def __str__(self) -> str:
        return self.value


#: dispatch priority, lower goes first (§4.4: alert > warn > info)
SEVERITY_RANK: Mapping[Severity, int] = {
    Severity.ALERT: 0,
    Severity.WARN: 1,
    Severity.INFO: 2,
}


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    project: str
    kind: str
    severity: Severity
    dedup_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        project: str,
        kind: str,
        *,
        severity: Severity | str = Severity.INFO,
        dedup_key: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "Event":
        return cls(
            id=-1,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            project=project,
            kind=kind,
            severity=Severity(severity),
            dedup_key=dedup_key or f"{project}:{kind}",
            payload=dict(payload or {}),
        )


def emit(
    project: str,
    kind: str,
    *,
    severity: Severity | str = Severity.INFO,
    dedup_key: str | None = None,
    **payload: Any,
) -> int | None:
    """Append one event to the ledger; return its id, or None if dropped.

    Never raises by contract (§4.2). ``dedup_key`` defaults to
    ``project:kind`` and may be narrowed per source (e.g. one silent WS
    stream out of several).
    """
    try:
        from . import ledger  # deferred: import of ledger must not gate emit

        event = Event.new(
            project, kind, severity=severity, dedup_key=dedup_key, payload=payload
        )
        return ledger.default().insert(event)
    except Exception as exc:  # noqa: BLE001 — the never-raises contract
        print(f"notify: emit failed, event dropped ({exc})", file=sys.stderr)
        return None
