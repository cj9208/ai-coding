"""The adapter interface and the one honest renderer.

Payloads stay machine-readable in the ledger; wording is born here (§4.1),
so a channel can change its voice without touching a single emitter.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..events import Event


class ChannelError(RuntimeError):
    """Delivery failed — the caller records it in ``deliveries``."""


class Channel(Protocol):
    name: str

    def send(self, event: Event) -> None:
        """Deliver one event; raise ChannelError on failure."""
        ...


def render(event: Event) -> str:
    """One line: ts [severity] project.kind {payload}."""
    body = json.dumps(event.payload, ensure_ascii=False, default=str, sort_keys=True)
    return f"{event.ts} [{event.severity}] {event.project}.{event.kind} {body}"
