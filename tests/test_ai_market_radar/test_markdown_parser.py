from ai_market_radar.fetchers.markdown import parse_changelog
from ai_market_radar.models import SourceConfig

MD_SOURCE = SourceConfig(
    key="md",
    name="MD Docs",
    entity="openai",
    signal="product",
    kind="markdown",
    url="https://example.com/changelog.md",
)


def test_anthropic_style_full_dates():
    text = """---
title: Notes
---
# Claude Platform release notes

### September 3, 2026
**New model: Claude Agent**
- Body text here.

### August 27, 2026
**Tool use update**
- Another body.
"""
    entries = parse_changelog(text, MD_SOURCE)
    assert [e.title for e in entries] == ["New model: Claude Agent", "Tool use update"]
    assert [e.published_at for e in entries] == ["2026-09-03", "2026-08-27"]
    assert "Another body" in entries[1].excerpt


def test_openai_style_month_groups_with_partial_dates():
    text = """# Changelog
## September, 2026
### Sep 3
**gpt-5.1-mini** is now available.
### Sep 3
**Responses API** adds a feature.
### Sep 1
**Audio model** update.
## August, 2026
### Aug 29
**Vision support** shipped.
"""
    entries = parse_changelog(text, MD_SOURCE)
    assert len(entries) == 4
    assert entries[0].title == "gpt-5.1-mini"
    assert entries[0].published_at == "2026-09-03"
    assert entries[1].title == "Responses API"
    assert entries[1].published_at == "2026-09-03"
    assert entries[3].published_at == "2026-08-29"


def test_claude_code_version_entries():
    text = """# Changelog
## 2.1.261
- Added an organization policy line.
- Fixed proxy handling.
## 2.1.260
- Bug fixes.
"""
    entries = parse_changelog(text, MD_SOURCE)
    assert [e.title for e in entries] == ["2.1.261", "2.1.260"]
    assert all(e.published_at is None for e in entries)
    assert "proxy handling" in entries[0].excerpt


def test_undated_reference_page_falls_back_to_whole_document():
    text = """# Models
## gpt-5.1
State of the art coding model.
## gpt-4.1
Previous generation.
"""
    entries = parse_changelog(text, MD_SOURCE)
    assert len(entries) == 1
    assert entries[0].title == MD_SOURCE.name
