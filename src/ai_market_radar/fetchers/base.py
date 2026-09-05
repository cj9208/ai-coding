from __future__ import annotations

import hashlib
import html
import re

import httpx

USER_AGENT = "ai-market-radar/0.1 (official-source knowledge base scanner)"
TIMEOUT = httpx.Timeout(25.0, connect=10.0)

_TAG_RE = re.compile(r"<[^>]+>")


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    return html.unescape(re.sub(r"\s+", " ", cleaned)).strip()


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
