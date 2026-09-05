from __future__ import annotations

import calendar
from datetime import datetime, timezone

import feedparser

from ai_market_radar.fetchers.base import strip_html
from ai_market_radar.models import RawEntry, SourceConfig


def fetch_rss(source: SourceConfig, client) -> list[RawEntry]:
    response = client.get(source.url)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"RSS parse error: {parsed.bozo_exception!r}")

    entries: list[RawEntry] = []
    for entry in parsed.entries:
        title = strip_html(entry.get("title", ""))
        if not title:
            continue
        link = entry.get("link") or entry.get("id") or None
        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = None
        if published_parsed:
            published_at = datetime.fromtimestamp(
                calendar.timegm(published_parsed), tz=timezone.utc
            ).isoformat()
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        tags = tuple(
            tag.get("term", "").strip()
            for tag in (entry.get("tags") or [])
            if tag.get("term")
        )
        entries.append(
            RawEntry(
                title=title,
                url=link,
                published_at=published_at,
                excerpt=summary[:2000],
                tags=tags,
            )
        )
    return entries
