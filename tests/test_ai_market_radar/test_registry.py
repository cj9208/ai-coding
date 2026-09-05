from ai_market_radar.registry import load_sources


def test_registry_loads_all_sources():
    sources = load_sources()
    keys = {s.key for s in sources}
    assert {
        "openai_news",
        "openai_changelog",
        "anthropic_news",
        "github_blog",
        "copilot_plans",
    } <= keys
    assert len(sources) == len(keys)
    for s in sources:
        assert s.entity in ("openai", "anthropic", "copilot")
        assert s.kind in ("rss", "markdown", "html_news", "html_snapshot")
