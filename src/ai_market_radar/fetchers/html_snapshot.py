from __future__ import annotations

import re

from ai_market_radar.fetchers.base import collapse, sha256
from ai_market_radar.models import RawEntry, SourceConfig

_SKIP_BLOCK_RE = re.compile(
    r"<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def _readable_text(response) -> str:
    text = _SKIP_BLOCK_RE.sub(" ", response.text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|h[1-6]|tr|div)>", "\n", text, flags=re.IGNORECASE)
    return collapse(re.sub(r"<[^>]+>", " ", text))


def fetch_html_snapshot(source: SourceConfig, client) -> list[RawEntry]:
    """Snapshot a pricing/plans page; content hash changes surface deal updates."""
    response = client.get(source.url)
    response.raise_for_status()
    text = _readable_text(response)
    if len(text) < 50:
        raise RuntimeError(f"page text unexpectedly small ({len(text)} chars)")
    return [
        RawEntry(
            title=source.name,
            url=source.url,
            excerpt=text[:2000],
            hash_value=sha256(text),
        )
    ]
