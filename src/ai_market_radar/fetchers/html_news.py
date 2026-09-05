from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlsplit

from ai_market_radar.fetchers.base import collapse
from ai_market_radar.models import RawEntry, SourceConfig
from ai_market_radar.normalize import fingerprint_url

_HREF_RE = re.compile(r'href="(/news/[a-zA-Z0-9][\w\-/]*?)"')
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']'
)
_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']'
)

_MAX_ARTICLES = 25


def _humanize(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _slug_from_path(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def fetch_news_index(
    source: SourceConfig, client, known_urls: set[str], limit: int = _MAX_ARTICLES
) -> list[RawEntry]:
    """Read the Anthropic /news index and enrich article pages for unseen slugs."""
    response = client.get(source.url)
    response.raise_for_status()

    slugs: list[str] = []
    for href in _HREF_RE.findall(response.text):
        path = href.rstrip("/")
        if path not in slugs:
            slugs.append(path)
    if not slugs:
        raise RuntimeError(
            "no /news/ links found on index page; structure may have changed"
        )

    split = urlsplit(source.url)
    origin = f"{split.scheme}://{split.netloc}"
    new_paths = [
        path for path in slugs if fingerprint_url(f"{origin}{path}") not in known_urls
    ]
    entries: list[RawEntry] = []
    for path in new_paths[:limit]:
        url = f"{origin}{path}"
        title, description = _article_meta(client, url)
        entries.append(
            RawEntry(
                title=title or _humanize(_slug_from_path(path)),
                url=url,
                excerpt=description,
            )
        )
    return entries


def _article_meta(client, url: str) -> tuple[str | None, str]:
    try:
        response = client.get(url)
        response.raise_for_status()
        text = response.text
    except Exception:
        return None, ""
    title = None
    desc = ""
    m = _OG_TITLE_RE.search(text)
    if m:
        title = collapse(unescape(m.group(1)))
    m = _OG_DESC_RE.search(text)
    if m:
        desc = collapse(unescape(m.group(1)))[:500]
    return title, desc
