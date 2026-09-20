"""Source adapters (02 §3): one interface, pluggable backends.

    class SourceAdapter(Protocol):
        kind: str
        async def search(self, q: str, k: int) -> list[Hit]
        async def fetch(self, hit: Hit) -> Fetched

Collector is a thin dispatcher over these; retries and failure recording live
in the collector, not the adapters — an adapter raises, the pipeline decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AdapterError(RuntimeError):
    """Network/parse failure. The research agent never hard-fails a session
    for one dead URL (02 §3): collector records it as a SourceFailure."""


@dataclass(frozen=True)
class Hit:
    url: str
    title: str = ""
    snippet: str = ""


@dataclass(frozen=True)
class Fetched:
    url: str
    text: str
    title: str = ""


class SourceAdapter(Protocol):
    kind: str

    async def search(self, q: str, k: int) -> list[Hit]: ...

    async def fetch(self, hit: Hit) -> Fetched: ...
