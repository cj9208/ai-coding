from ai_market_radar.models import RawEntry, SourceConfig
from ai_market_radar.normalize import (
    classify_signal,
    fingerprint_for,
    fingerprint_url,
    to_items,
)


def source(**overrides) -> SourceConfig:
    base = dict(
        key="s",
        name="S",
        entity="openai",
        signal="product",
        kind="rss",
        url="https://x.dev/a",
    )
    base.update(overrides)
    return SourceConfig.from_dict(base)


def test_fingerprint_url_normalizes_host_and_drops_fragment():
    a = fingerprint_url("https://OpenAI.com/news/x?utm=1#frag")
    b = fingerprint_url("http://openai.com/news/x/")
    assert a == b == "url:openai.com/news/x"


def test_markdown_and_snapshot_fingerprints_are_content_hash_based():
    entry = RawEntry(title="t", url="https://x.dev/a", hash_value="abc123")
    md = fingerprint_for(source(kind="markdown"), entry, "product")
    snap = fingerprint_for(source(kind="html_snapshot"), entry, "deal")
    assert md == "hash:abc123"
    assert snap == "hash:abc123"


def test_classify_signals():
    assert classify_signal(source(), "Pricing update: new API tier", "rss") == "deal"
    assert (
        classify_signal(source(signal="research"), "New model research update", "rss")
        == "research"
    )
    assert classify_signal(source(), "Introducing a new model", "rss") == "research"
    assert (
        classify_signal(
            source(kind="html_snapshot", signal="product"), "Anything", "html_snapshot"
        )
        == "deal"
    )


def test_filter_tags_keeps_only_relevant_feeds():
    cfg = source(filter={"tags_include": ["copilot"]})
    keep = RawEntry(title="Copilot code review", tags=("GitHub Copilot",))
    drop = RawEntry(title="Dependabot update", tags=("Security",))
    items = to_items(cfg, [keep, drop])
    assert len(items) == 1
    assert items[0].title == "Copilot code review"
