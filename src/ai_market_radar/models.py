from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ENTITIES = ("openai", "anthropic", "copilot")
SIGNALS = ("research", "product", "deal", "status")
KINDS = ("rss", "markdown", "html_news", "html_snapshot")


@dataclass
class SourceConfig:
    key: str
    name: str
    entity: str
    signal: str
    kind: str
    url: str
    enabled: bool = True
    filter_tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "SourceConfig":
        data = dict(raw)
        data.setdefault("enabled", True)
        data.setdefault("signal", "product")
        data.setdefault("kind", "rss")
        filt = data.pop("filter", None) or {}
        data["filter_tags"] = tuple(filt.get("tags_include", ()))
        return cls(**data)


@dataclass
class RawEntry:
    title: str
    url: Optional[str] = None
    published_at: Optional[str] = None
    excerpt: str = ""
    tags: tuple[str, ...] = ()
    hash_value: Optional[str] = None


@dataclass
class Item:
    source_key: str
    entity: str
    signal: str
    title: str
    url: Optional[str]
    published_at: Optional[str]
    excerpt: str
    fingerprint: str
    fetched_at: str
    raw_hash: str
    tag: str = "feature"
