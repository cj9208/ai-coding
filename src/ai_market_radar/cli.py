"""``ai-market-radar`` CLI — one click command, no subcommands.

``--list-sources`` is an eager option: it prints the registry and exits
before any scan runs, mirroring the old flag check that led ``main``.
Where the old code did ``raise SystemExit("message")`` (stderr, exit 1)
the commands now raise ``click.ClickException`` (stderr "Error: message",
exit 1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from ai_market_radar.digest import count_summary, save_digest
from ai_market_radar.fetchers import fetch_source
from ai_market_radar.fetchers.base import make_client
from ai_market_radar.models import Item, SourceConfig
from ai_market_radar.normalize import to_items
from ai_market_radar.registry import load_sources
from ai_market_radar.store import KnowledgeBase

# repo-root anchor per AGENTS.md, defined once in utils.paths:
# the KB lives under data/ai_market_radar no matter where the CLI is launched
from utils import paths

DEFAULT_DATA_DIR = paths.data_dir("ai_market_radar")


def _print_registry_and_exit(
    ctx: click.Context, param: click.Parameter, value: bool
) -> bool:
    """Eager callback for --list-sources: print the registry and exit 0."""
    if not value or ctx.resilient_parsing:
        return value
    for source in load_sources():
        state = "on" if source.enabled else "off"
        click.echo(
            f"{source.key:<24} {state:<3} {source.entity:<10}"
            f" {source.kind:<12} {source.url}"
        )
    ctx.exit(0)
    return value


def _selected(
    sources: list[SourceConfig], keys: list[str] | None
) -> list[SourceConfig]:
    if not keys:
        return [s for s in sources if s.enabled]
    wanted = set(keys)
    selected = [s for s in sources if s.key in wanted]
    missing = wanted - {s.key for s in selected}
    if missing:
        raise click.ClickException(
            f"unknown source key(s): {', '.join(sorted(missing))}"
        )
    return selected


def _print_summary(inserted: list[Item], errors: list[str]) -> None:
    if errors:
        print("Failures (see source_state in KB):")
        for error in errors:
            print(f"  - {error}")
    if not inserted:
        print("No new items.")
        return
    summary = count_summary(inserted)
    for entity, counts in summary.items():
        parts = ", ".join(f"{signal}={n}" for signal, n in counts.items())
        print(f"{entity}: {parts}")


def run_scan(data_dir: Path, keys: list[str] | None, max_articles: int) -> dict:
    sources = load_sources()
    selected = _selected(sources, keys)
    if not selected:
        raise click.ClickException("no sources selected (all disabled?)")

    kb_path = data_dir / "kb.db"
    digest_dir = data_dir / "digests"
    kb = KnowledgeBase(kb_path)
    errors: list[str] = []
    inserted: list[Item] = []
    run_at = datetime.now(timezone.utc)

    try:
        with make_client() as client:
            known_urls = kb.known_url_fingerprints()
            for source in selected:
                label = f"[{source.key}] {source.name}"
                try:
                    entries = fetch_source(source, client, known_urls, max_articles)
                    items = to_items(source, entries)
                    new_count = 0
                    for item in items:
                        if kb.insert(item):
                            inserted.append(item)
                            new_count += 1
                    kb.set_source_ok(source.key, len(items), new_count)
                    print(f"ok   {label}: {len(items)} items, {new_count} new")
                except Exception as exc:  # noqa: BLE001 - isolate per source
                    kb.set_source_error(source.key, str(exc))
                    errors.append(f"{label}: {exc}")
                    print(f"fail {label}: {exc}")
    finally:
        kb.close()

    digest_path = save_digest(digest_dir, inserted, run_at)
    print("")
    _print_summary(inserted, errors)
    if digest_path:
        print(f"digest: {digest_path}")
    return {"inserted": len(inserted), "errors": errors}


@click.command("ai-market-radar")
@click.option(
    "--data-dir",
    type=Path,
    default=DEFAULT_DATA_DIR,
    help="directory for the knowledge base and digests",
)
@click.option("--source", multiple=True, help="only scan this source key (repeatable)")
@click.option(
    "--list-sources",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_print_registry_and_exit,
    help="print the source registry and exit",
)
@click.option(
    "--max-articles",
    type=int,
    default=25,
    help="max article pages to enrich per html_news run",
)
def cli(data_dir: Path, source: tuple[str, ...], max_articles: int) -> None:
    """Scan official OpenAI / Anthropic / GitHub Copilot sources into a
    knowledge base and report new developments."""
    run_scan(data_dir, list(source) if source else None, max_articles)


if __name__ == "__main__":
    cli()
