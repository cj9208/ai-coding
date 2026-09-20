"""web_search via DuckDuckGo's HTML endpoint — no API key, good enough for MVP.

Deliberately boring parsing of the `html.duckduckgo.com` markup; when the
page shape changes this raises AdapterError and the collector records a
source failure rather than guessing. Swap the backend by passing another
adapter with kind="web_search" (e.g. a real SERP API) — nothing upstream
knows about DDG.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .base import AdapterError, Fetched, Hit
from .page_fetch import PageFetchAdapter

_ENDPOINT = "https://html.duckduckgo.com/html/"
_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?P<rest>.*?)(?=<a[^>]+class="result__a"|\Z)',
    re.S,
)
_TAG = re.compile(r"<[^>]+>")


class WebSearchAdapter:
    kind = "web_search"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._fetcher = PageFetchAdapter(client=client)

    async def search(self, q: str, k: int) -> list[Hit]:
        client = self._client or httpx.AsyncClient(timeout=25, follow_redirects=True)
        owns = self._client is None
        try:
            resp = await client.post(
                _ENDPOINT,
                data={"q": q},
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-agent/1.0"
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AdapterError(f"search failed: {exc}") from exc
        finally:
            if owns:
                await client.aclose()
        hits = _parse_results(resp.text, k)
        if not hits:
            raise AdapterError(
                "search returned no parseable results "
                "(markup changed or endpoint blocked)"
            )
        return hits

    async def fetch(self, hit: Hit) -> Fetched:
        return await self._fetcher.fetch(hit)


def _parse_results(html: str, k: int) -> list[Hit]:
    hits: list[Hit] = []
    for m in _RESULT.finditer(html):
        url = _real_url(m.group("href"))
        if not url or hits_and_skip(url):
            continue
        title = _TAG.sub("", m.group("title")).strip()
        snip = re.search(
            r'class="result__snippet"[^>]*>(.*?)</a>', m.group("rest"), re.S
        )
        snippet = _TAG.sub("", snip.group(1)).strip() if snip else ""
        hit = Hit(url=url, title=title or url, snippet=snippet[:400])
        if hit not in hits:
            hits.append(hit)
        if len(hits) >= k:
            break
    return hits


def _real_url(href: str) -> str:
    """DDG wraps results as /l/?uddg=<encoded>; unwrap, else return as-is."""
    if "uddg=" in href:
        try:
            qs = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            return unquote(qs["uddg"][0])
        except (KeyError, IndexError, ValueError):
            return ""
    if href.startswith("//"):
        return "https:" + href
    return href if href.startswith("http") else ""


def hits_and_skip(url: str) -> bool:
    return "duckduckgo.com" in url or url.startswith("https://html.")
