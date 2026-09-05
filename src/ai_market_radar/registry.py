from __future__ import annotations

import os
import tomllib
from pathlib import Path

from ai_market_radar.models import SourceConfig

_DEFAULT_SOURCES = Path(__file__).parent / "sources.toml"


def load_sources(path: str | Path | None = None) -> list[SourceConfig]:
    if path is None:
        path = os.environ.get("AI_MARKET_RADAR_SOURCES") or _DEFAULT_SOURCES
    with open(Path(path), "rb") as handle:
        data = tomllib.load(handle)
    sources = [SourceConfig.from_dict(raw) for raw in data.get("source", [])]
    known = {s.key for s in sources}
    if len(known) != len(sources):
        raise ValueError("duplicate source keys in registry")
    return sources
