from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from ai_market_radar.fetchers.base import sha256
from ai_market_radar.models import Item, RawEntry, SourceConfig
from ai_market_radar.tags import tag_item

_DEAL_RE = re.compile(
    r"(price|pricing|\$\s?\d|per\s+month|\bplan\b|plans|subscription|credit|"
    r"discount|promo|offer|free tier|tokens included|rate limit)",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"(research|paper|arxiv|benchmark|frontier|alignment|new model|model card|"
    r"capabilit|swat|interpretab|multimodal|reasoning model)",
    re.IGNORECASE,
)
_PRODUCT_RE = re.compile(
    r"(launch|release|feature|update|api|sdk|app|available now)", re.IGNORECASE
)


def classify_signal(source: SourceConfig, title: str, kind: str) -> str:
    signal = source.signal
    if kind in ("html_snapshot",):
        return "deal"
    if _DEAL_RE.search(title):
        return "deal"
    if signal == "research" and _RESEARCH_RE.search(title):
        return "research"
    if (
        signal == "product"
        and _RESEARCH_RE.search(title)
        and not _DEAL_RE.search(title)
    ):
        return "research"
    if signal == "research" and _PRODUCT_RE.search(title):
        return "product"
    return signal


def fingerprint_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    return f"url:{host}{path}"


def fingerprint_for(source: SourceConfig, entry: RawEntry, signal: str) -> str:
    if source.kind in ("html_snapshot", "markdown"):
        return f"hash:{entry.hash_value or sha256(entry.title)}"
    if entry.url:
        return fingerprint_url(entry.url)
    return f"hash:{entry.hash_value or sha256(entry.title)}"


def _passes_filter(source: SourceConfig, entry: RawEntry) -> bool:
    if not source.filter_tags:
        return True
    haystack = " ".join([entry.title, *entry.tags]).lower()
    return any(tag.lower() in haystack for tag in source.filter_tags)


def to_item(source: SourceConfig, entry: RawEntry, fetched_at: str) -> Item:
    signal = classify_signal(source, entry.title, source.kind)
    return Item(
        source_key=source.key,
        entity=source.entity,
        signal=signal,
        title=entry.title,
        url=entry.url,
        published_at=entry.published_at,
        excerpt=entry.excerpt,
        fingerprint=fingerprint_for(source, entry, signal),
        fetched_at=fetched_at,
        raw_hash=entry.hash_value or sha256(entry.title),
        tag=tag_item(source, entry.title),
    )


def to_items(source: SourceConfig, entries: list[RawEntry]) -> list[Item]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    items: list[Item] = []
    for entry in entries:
        if not _passes_filter(source, entry):
            continue
        items.append(to_item(source, entry, fetched_at))
    return items
