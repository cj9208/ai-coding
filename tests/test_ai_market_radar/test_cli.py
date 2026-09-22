"""ai-market-radar CLI — source selection, scan wiring, summary printing.

Network is out of scope: fetcher/parser internals have their own tests, so
here the fetch step is a stand-in and everything around it (KB dedupe,
digesting, exit behavior) runs for real.
"""

from contextlib import nullcontext
from dataclasses import replace

import click
import pytest
from click.testing import CliRunner

from ai_market_radar import cli
from ai_market_radar.models import Item, SourceConfig


def _source(key: str = "openai_news", enabled: bool = True) -> SourceConfig:
    return SourceConfig(
        key=key,
        name="OpenAI News",
        entity="OpenAI",
        signal="product",
        kind="rss",
        url="https://example.com/feed",
        enabled=enabled,
    )


def _item(fingerprint: str = "fp1") -> Item:
    return Item(
        source_key="openai_news",
        entity="OpenAI",
        signal="product",
        title="t",
        url="https://example.com/a",
        published_at=None,
        excerpt="",
        fingerprint=fingerprint,
        fetched_at="2026-09-21T00:00:00+00:00",
        raw_hash="rh",
    )


def test_selected_defaults_to_enabled_only():
    sources = [_source("a"), _source("b", enabled=False)]

    assert [s.key for s in cli._selected(sources, None)] == ["a"]


def test_selected_explicit_keys_bypass_enabled_but_reject_unknown():
    sources = [_source("a"), _source("b", enabled=False)]

    assert [s.key for s in cli._selected(sources, ["b"])] == ["b"]

    with pytest.raises(click.ClickException, match="unknown source key"):
        cli._selected(sources, ["nope"])


def test_print_summary_groups_by_entity(capsys):
    cli._print_summary([_item(), _item("fp2")], errors=["[x] boom"])

    out = capsys.readouterr().out
    assert "boom" in out
    assert "OpenAI: product=2" in out

    cli._print_summary([], errors=[])
    assert "No new items." in capsys.readouterr().out


def _wire_cli(monkeypatch, items):
    monkeypatch.setattr(cli, "load_sources", lambda: [_source()])
    monkeypatch.setattr(cli, "make_client", lambda: nullcontext(object()))
    monkeypatch.setattr(cli, "fetch_source", lambda *a, **k: ["entry"])
    monkeypatch.setattr(cli, "to_items", lambda source, entries: list(items))


def test_run_scan_dedupes_against_the_kb(monkeypatch, tmp_path, capsys):
    _wire_cli(monkeypatch, [_item()])

    first = cli.run_scan(tmp_path, None, 25)
    assert first["inserted"] == 1 and first["errors"] == []
    assert (tmp_path / "digests").exists()  # a digest was written for new items

    second = cli.run_scan(tmp_path, None, 25)  # same fingerprint already stored
    assert second["inserted"] == 0
    out = capsys.readouterr().out
    assert "No new items." in out
    assert (tmp_path / "kb.db").exists()


def test_run_scan_isolates_a_failing_source(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("feed 503")

    monkeypatch.setattr(cli, "load_sources", lambda: [_source()])
    monkeypatch.setattr(cli, "make_client", lambda: nullcontext(object()))
    monkeypatch.setattr(cli, "fetch_source", boom)

    res = cli.run_scan(tmp_path, None, 25)

    assert res["inserted"] == 0
    assert any("feed 503" in e for e in res["errors"])


def test_run_scan_refuses_an_all_disabled_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "load_sources", lambda: [replace(_source(), enabled=False)]
    )

    with pytest.raises(click.ClickException, match="no sources selected"):
        cli.run_scan(tmp_path, None, 25)


def test_cli_list_sources_prints_the_real_registry():
    result = CliRunner().invoke(cli.cli, ["--list-sources"])

    assert result.exit_code == 0, result.output
    out = result.stdout
    printed_keys = {line.split()[0] for line in out.splitlines() if line.strip()}
    assert {s.key for s in cli.load_sources()} <= printed_keys
