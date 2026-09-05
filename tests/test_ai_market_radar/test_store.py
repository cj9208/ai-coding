from datetime import datetime, timezone

from ai_market_radar.models import Item
from ai_market_radar.store import KnowledgeBase


def _item(fingerprint: str, title: str = "t", url: str | None = None) -> Item:
    return Item(
        source_key="s",
        entity="openai",
        signal="product",
        title=title,
        url=url,
        published_at=None,
        excerpt="x",
        fingerprint=fingerprint,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_hash="h",
    )


def test_dedupe_via_unique_fingerprint(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.db")
    try:
        assert kb.insert(_item("url:a.dev/x")) is True
        assert kb.insert(_item("url:a.dev/x")) is False
        assert kb.insert(_item("url:a.dev/y")) is True
        assert kb.known_url_fingerprints() == {"url:a.dev/x", "url:a.dev/y"}
    finally:
        kb.close()


def test_source_state_records_errors(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.db")
    try:
        kb.set_source_ok("s1", 5, 2)
        kb.set_source_error("s1", "boom")
    finally:
        kb.close()
