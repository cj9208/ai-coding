from datetime import datetime, timezone

from ai_market_radar.digest import render_digest, save_digest
from ai_market_radar.models import Item


def _item(entity: str, tag: str, title: str, published_at: str | None = None) -> Item:
    return Item(
        source_key="s",
        entity=entity,
        signal="product",
        tag=tag,
        title=title,
        url="https://x.dev/1",
        published_at=published_at,
        excerpt="a short excerpt here",
        fingerprint=f"url:x.dev/{title}",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_hash="h",
    )


def test_render_digest_groups_by_company_then_tag_in_priority_order():
    items = [
        _item("openai", "pricing", "ChatGPT Pro price cut", "2026-09-04"),
        _item("openai", "model", "Introducing GPT-5.6", "2026-09-05"),
        _item("openai", "ecosystem", "How Acme builds with Codex"),
        _item("anthropic", "feature", "Claude Code improvements"),
    ]
    out = render_digest(items, datetime(2026, 9, 5, 8, 30, tzinfo=timezone.utc))
    assert out.startswith("# AI Market Radar — 2026-09-05 08:30 UTC")
    assert "New this run — OpenAI 3 · Anthropic 1." in out
    assert "## OpenAI (3)" in out
    assert "## Anthropic (1)" in out
    assert "### New models & capabilities" in out
    assert "### Pricing & offers" in out
    assert "### Customers & partnerships" in out
    assert out.index("### New models & capabilities") < out.index(
        "### Pricing & offers"
    )
    assert "**Introducing GPT-5.6**" in out


def test_render_digest_empty():
    out = render_digest([], datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert "No new items." in out
    assert "## " not in out


def test_save_digest_files_are_per_run_with_minute_precision(tmp_path):
    items = [_item("openai", "model", "Introducing GPT-5.6")]
    run_morning = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
    run_lunch = datetime(2026, 9, 5, 12, 15, tzinfo=timezone.utc)

    first = save_digest(tmp_path, items, run_morning)
    assert first is not None and first.endswith("2026-09-05-0800.md")

    same_minute_rerun = save_digest(tmp_path, items, run_morning)
    assert same_minute_rerun is None  # identical run does not rewrite

    later_run = save_digest(
        tmp_path, [_item("anthropic", "pricing", "Claude pricing cut")], run_lunch
    )
    assert later_run is not None and later_run.endswith("2026-09-05-1215.md")

    morning = (tmp_path / "2026-09-05-0800.md").read_text(encoding="utf-8")
    lunch = (tmp_path / "2026-09-05-1215.md").read_text(encoding="utf-8")
    assert morning.count("**Introducing GPT-5.6**") == 1
    assert "**Introducing GPT-5.6**" not in lunch
    assert "**Claude pricing cut**" in lunch
