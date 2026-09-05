from __future__ import annotations

from ai_market_radar.fetchers.html_news import fetch_news_index
from ai_market_radar.fetchers.html_snapshot import fetch_html_snapshot
from ai_market_radar.fetchers.markdown import fetch_markdown
from ai_market_radar.fetchers.rss import fetch_rss
from ai_market_radar.models import RawEntry, SourceConfig


def fetch_source(
    source: SourceConfig,
    client,
    known_urls: set[str] | None = None,
    max_articles: int = 25,
) -> list[RawEntry]:
    if source.kind == "rss":
        return fetch_rss(source, client)
    if source.kind == "markdown":
        return fetch_markdown(source, client)
    if source.kind == "html_news":
        return fetch_news_index(source, client, known_urls or set(), max_articles)
    if source.kind == "html_snapshot":
        return fetch_html_snapshot(source, client)
    raise ValueError(f"unknown source kind: {source.kind!r}")
