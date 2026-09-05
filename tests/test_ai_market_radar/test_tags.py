from ai_market_radar.models import SourceConfig
from ai_market_radar.tags import SECTION_ORDER, tag_item


def source(**overrides) -> SourceConfig:
    base = dict(
        key="s",
        name="S",
        entity="openai",
        signal="product",
        kind="rss",
        url="https://x.dev",
    )
    base.update(overrides)
    return SourceConfig.from_dict(base)


def test_tag_rules_examples():
    cases = [
        ("Introducing GPT-5.6", "model"),
        ("OpenAI frontier models and Codex are now available on AWS", "model"),
        ("ChatGPT Pro price changes", "pricing"),
        ("Claude pricing cut", "pricing"),
        ("Anthropic cuts API prices", "pricing"),
        ("Pushing on code execution, now available in the API", "feature"),
        ("Advancing voice intelligence with new models in the API", "feature"),
        ("Introducing FrontierScience benchmark", "research"),
        ("Path to Astra: critical capabilities and frontier safeguards", "security"),
        ("How HSP GRUPPE builds AI capabilities for tax advisory", "ecosystem"),
        ("Snowflake and OpenAI partner to bring frontier intelligence", "ecosystem"),
        ("OpenAI announces Frontier Alliance Partners", "ecosystem"),
        ("Introducing Verdi, an AI dev platform powered by GPT-4o", "ecosystem"),
        ("Introducing the GPT Store", "feature"),
        ("Introducing the Model Spec", "feature"),
        (
            "Introducing improvements to the fine-tuning API and custom models program",
            "feature",
        ),
        ("Incident: elevated errors", "status"),
    ]
    for title, expected in cases:
        signal = "status" if expected == "status" else "product"
        assert tag_item(source(signal=signal), title) == expected, title


def test_tag_fallback_uses_signal_when_title_is_ambiguous():
    assert tag_item(source(signal="product"), "Weekly roundup") == "feature"
    assert tag_item(source(signal="research"), "Weekly roundup") == "research"
    assert tag_item(source(signal="deal"), "Weekly roundup") == "pricing"


def test_status_sources_always_tag_status():
    cfg = source(signal="status", kind="rss")
    assert tag_item(cfg, "History of incidents") == "status"


def test_snapshot_always_tag_pricing():
    cfg = source(kind="html_snapshot", signal="product")
    assert tag_item(cfg, "Plans page") == "pricing"


def test_section_order_is_stable_and_complete():
    tags = [t for t, _ in SECTION_ORDER]
    assert tags == [
        "model",
        "pricing",
        "feature",
        "research",
        "security",
        "company",
        "ecosystem",
        "status",
    ]
