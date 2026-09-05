from __future__ import annotations

from datetime import datetime

from ai_market_radar.models import ENTITIES, Item
from ai_market_radar.tags import SECTION_ORDER

ENTITY_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "copilot": "GitHub Copilot",
}


def _clean_excerpt(excerpt: str, limit: int = 160) -> str:
    text = " ".join(excerpt.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _bullet(item: Item) -> str:
    date = f" · {item.published_at[:10]}" if item.published_at else ""
    url = f" <{item.url}>" if item.url else ""
    excerpt = f" — {_clean_excerpt(item.excerpt)}" if item.excerpt else ""
    return f"- **{item.title}**{url}{date}{excerpt}"


def _sort_items(items: list[Item]) -> list[Item]:
    return sorted(
        items,
        key=lambda it: (it.published_at is None, it.published_at or "", it.title),
    )


def render_digest(items: list[Item], run_at: datetime) -> str:
    by_entity: dict[str, list[Item]] = {}
    for item in items:
        by_entity.setdefault(item.entity, []).append(item)

    lines = [f"# AI Market Radar — {run_at:%Y-%m-%d %H:%M} UTC", ""]
    total = len(items)
    if total:
        parts = []
        for entity in ENTITIES:
            if entity in by_entity:
                parts.append(f"{ENTITY_LABELS[entity]} {len(by_entity[entity])}")
        lines.append("New this run — " + " · ".join(parts) + ".")
    else:
        lines.append("No new items.")
    lines.append("")

    for entity in ENTITIES:
        entity_items = by_entity.get(entity)
        if not entity_items:
            continue
        lines.append(f"## {ENTITY_LABELS[entity]} ({len(entity_items)})")
        lines.append("")
        for tag, label in SECTION_ORDER:
            tagged = [it for it in entity_items if it.tag == tag]
            if not tagged:
                continue
            lines.append(f"### {label}")
            lines.append("")
            for item in _sort_items(tagged):
                lines.append(_bullet(item))
            lines.append("")
    return "\n".join(lines).rstrip()


def count_summary(items: list[Item]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for item in items:
        summary.setdefault(item.entity, {}).setdefault(item.signal, 0)
        summary[item.entity][item.signal] += 1
    return summary


def save_digest(digest_dir, items: list[Item], run_at: datetime) -> str | None:
    if not items:
        return None
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{run_at:%Y-%m-%d-%H%M}.md"
    content = render_digest(items, run_at)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing == content.strip():
            return None
        content = f"{existing}\n\n---\n\n{content}\n"
    else:
        content = f"{content}\n"
    path.write_text(content, encoding="utf-8")
    return str(path)
