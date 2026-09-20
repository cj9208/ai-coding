"""Persistence home for research_agent.

Renamed from ``research_agent.storage`` when the repo gained a top-level
shared ``storage`` package: absolute imports keep the two apart fine, but
``from ..storage...`` next to ``from storage import...`` in one codebase is
a reading hazard. Tables + store stay here (business), engine/PRAGMA/FTS
knowledge comes from the shared layer.
"""

from .db import Base
from .store import SessionStore

__all__ = ["Base", "SessionStore"]
