"""page_fetch: httpx + markdownify, body extraction with boilerplate stripped.

Truncation is deliberate: extraction quality plateaus long before a whole
forum thread, and the capture budget is per CALL, not per kilobyte.
"""

from __future__ import annotations

import re

import httpx
from markdownify import markdownify

from .base import AdapterError, Fetched, Hit

MAX_TEXT_CHARS = 12_000
_UA = "Mozilla/5.0 (X11; Linux x86_64) research-agent/1.0"
_DROP_TAGS = re.compile(
    r"<(script|style|nav|header|footer|aside|form|noscript)\b.*?</\1>",
    re.S | re.I,
)


class PageFetchAdapter:
    kind = "page_fetch"

    def __init__(
        self, client: httpx.AsyncClient | None = None, max_chars: int = MAX_TEXT_CHARS
    ):
        self._client = client
        self._max_chars = max_chars

    async def search(self, q: str, k: int) -> list[Hit]:
        raise AdapterError("page_fetch has no search; give it URLs via fetch(Hit)")

    async def fetch(self, hit: Hit) -> Fetched:
        client = self._client or httpx.AsyncClient(timeout=25, follow_redirects=True)
        owns = self._client is None
        try:
            resp = await client.get(hit.url, headers={"User-Agent": _UA})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AdapterError(f"fetch failed: {exc}") from exc
        finally:
            if owns:
                await client.aclose()
        html = resp.text
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type or html.lstrip().startswith(("<", "<!")):
            html = _DROP_TAGS.sub(" ", html)
            text = markdownify(html, heading="#", strip=["img", "button"])
        else:
            text = html
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        title = hit.title or _title_of(html)
        return Fetched(url=hit.url, text=text[: self._max_chars], title=title)


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
