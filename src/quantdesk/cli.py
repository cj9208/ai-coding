"""``quant`` CLI — the one entry point of the research data plane (click).

    quant download --datasets um_klines_1m --symbols BTCUSDT \
                   --since 2024-01 --until 2024-12       # diff-sync archives
    quant convert  --datasets um_klines_1m --symbols BTCUSDT \
                   --since 2024-01 --until 2024-12       # raw -> Parquet months
    quant list                                            # coverage table
    quant verify [--remote]                               # hash/grid/replacement
    quant universe sync|show|rank                         # dated symbol lists
    quant record [--streams …] [--minutes N]              # unarchived feeds
    quant screen --factor csm --rank 100 \
               --since 2022-01-01 --until 2026-06-30      # factor screening,
                                                           # holdout sealed

download/convert are pure "make local match the requested range"
commands — rerunning them is a no-op, matching the house posture that
provisioning is an idempotent CLI subcommand, not a shell script.

Declaration notes (post-migration from argparse, repo-wide click convention):
csv spellings like ``--datasets a,b`` are kept as the documented interface;
validation happens at parse time via callbacks, so a bad dataset name fails
with the legal list instead of mid-run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import click

from utils.paths import REPO_ROOT

from . import archive
from . import convert as convert_mod
from . import record
from . import screen as screen_mod
from . import store
from . import universe as universe_mod
from .config import DATASETS, parse_csv_list


def _default_until() -> str:
    last_month = date.today().replace(day=1) - timedelta(days=1)
    return last_month.strftime("%Y-%m")


def _csv_list(ctx: Any, param: Any, value: str) -> list[str]:
    """Split the documented csv spelling into a list at parse time."""
    return parse_csv_list(value)


def _csv_choices(names: tuple[str, ...]) -> Callable[..., list[str]]:
    """A csv list whose every value must be one of ``names`` — validated
    before any command body runs."""

    def cb(ctx: Any, param: Any, value: str) -> list[str]:
        picked = parse_csv_list(value)
        unknown = [n for n in picked if n not in names]
        if unknown:
            raise click.BadParameter(
                f"unknown: {', '.join(unknown)}; available: {', '.join(sorted(names))}"
            )
        return picked

    return cb


def _resolve_symbols(symbols: list[str], top: int, market: str) -> list[str]:
    if symbols:
        return symbols
    if top:
        snap = universe_mod.latest(market)
        if snap is None:
            raise click.ClickException(
                "no universe snapshot — run `quant universe sync`"
            )
        return snap.symbols[:top]
    raise click.UsageError("pass --symbols or --top N")


def range_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """Options shared by download and convert."""
    f = click.option(
        "--datasets",
        type=str,
        required=True,
        callback=_csv_choices(tuple(DATASETS)),
        metavar="CSV",
        help="csv of dataset names",
    )(f)
    f = click.option(
        "--symbols", default="", callback=_csv_list, help="csv of symbols"
    )(f)
    f = click.option("--market", type=click.Choice(["spot", "um"]), default="um")(f)
    f = click.option(
        "--top",
        type=int,
        default=0,
        help="first N from the latest universe snapshot (requires quant universe sync)",
    )(f)
    f = click.option("--since", required=True, help="YYYY-MM")(f)
    f = click.option("--until", default="", help="YYYY-MM (default: last month)")(f)
    return f


@click.group()
def cli() -> None:
    """research data plane over the Binance public archive"""


@cli.command()
@range_options
@click.option(
    "--daily/--no-daily",
    default=True,
    help="daily files for the not-yet-published current month",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="re-check upstream checksums and act on replacements",
)
def download(
    datasets: list[str],
    symbols: list[str],
    market: str,
    top: int,
    since: str,
    until: str,
    daily: bool,
    refresh: bool,
) -> None:
    """diff-sync archives into raw/ + inventory"""
    resolved = _resolve_symbols(symbols, top, market)
    counts = {"fetched": 0, "skipped": 0, "replaced": 0, "missing": 0}
    for name in datasets:
        actions = archive.download(
            DATASETS[name],
            resolved,
            since,
            until or _default_until(),
            include_daily=daily,
            refresh_remote=refresh,
        )
        for action in actions:
            counts[action.outcome] = counts.get(action.outcome, 0) + 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    click.echo(f"download: {summary}")


@cli.command()
@range_options
def convert(
    datasets: list[str],
    symbols: list[str],
    market: str,
    top: int,
    since: str,
    until: str,
) -> None:
    """build Parquet month partitions from raw"""
    resolved = _resolve_symbols(symbols, top, market)
    until = until or _default_until()
    built = 0
    for name in datasets:
        dataset = DATASETS[name]
        for symbol in resolved:
            for month in archive.months_between(since, until):
                result = convert_mod.convert_month(dataset, symbol, month)
                if result:
                    built += 1
                    click.echo(
                        f"built {result.dataset}/{result.symbol}/{result.month}"
                        f" rows={result.rows} from {result.source}"
                    )
    click.echo(f"convert: {built} month-partitions (re)built")


@cli.command("list")
def list_cmd() -> None:
    """coverage of built partitions"""
    rows = store.scan_coverage()
    if not rows:
        click.echo("no parquet partitions yet — run quant download + quant convert")
        return
    click.echo(f"{'dataset':<16} {'symbol':<12} {'months':<18} {'count':>5} rows")
    for row in rows:
        click.echo(
            f"{row.dataset:<16} {row.symbol:<12} {row.span:<18} "
            f"{len(row.months):>5} {row.rows}"
        )


@cli.command()
@click.option(
    "--remote",
    is_flag=True,
    help="also re-check every inventory file's upstream checksum",
)
def verify(remote: bool) -> None:
    """local hashes, time-grid continuity [, upstream replacement detector]"""
    issues = store.verify(remote=remote)
    for issue in issues:
        click.echo(f"{issue.kind:<11} {issue.subject}: {issue.detail}", err=True)
    click.echo(
        f"verify: {len(issues)} issue(s)" + (", --remote checked" if remote else "")
    )
    raise SystemExit(1 if issues else 0)


@cli.command("record")
@click.option(
    "--streams",
    default="liquidations,funding,open_interest",
    show_default=True,
)
@click.option("--symbols", default="", help="csv (per-symbol polls)")
@click.option(
    "--top", type=int, default=0, help="first N from the latest um universe snapshot"
)
@click.option(
    "--minutes",
    type=float,
    default=0.0,
    help="stop after N minutes (default: run until ^C)",
)
@click.option(
    "--silence-alert",
    type=float,
    default=300.0,
    show_default=True,
    help="log a gap when the liquidations stream pushes nothing for N seconds",
)
def record_stream(
    streams: str, symbols: str, top: int, minutes: float, silence_alert: float
) -> None:
    """accumulate liquidation/funding/OI feeds"""
    parsed = record.parse_streams(streams)
    if symbols:
        syms = parse_csv_list(symbols)
    elif top:
        snap = universe_mod.latest("um")
        if snap is None:
            raise click.ClickException(
                "no universe snapshot — run `quant universe sync`"
            )
        syms = snap.symbols[:top]
    else:
        syms = []
    if "open_interest" in parsed and not syms:
        raise click.UsageError(
            "open_interest needs --symbols or --top (per-symbol poll)"
        )
    state = record.RecorderState(
        symbols=syms,
        streams=parsed,
        minutes=minutes,
        silence_alert=silence_alert,
    )
    click.echo(
        f"recording {','.join(parsed)}"
        + (f" for {len(syms)} symbols" if syms else " (all-market flows)")
    )
    try:
        asyncio.run(record.run(state))
    except KeyboardInterrupt:
        click.echo("^C — final flushed", err=True)


def _parse_sets(pairs: tuple[str, ...]) -> dict:
    params: dict = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if raw.isdigit():
            params[key.strip()] = int(raw)
        else:
            params[key.strip()] = float(raw)
    return params


@cli.command()
@click.option("--factor", required=True, type=click.Choice(sorted(screen_mod.FACTORS)))
@click.option("--dataset", default="um_klines_1d", show_default=True)
@click.option("--symbols", default="", callback=_csv_list, help="csv of symbols")
@click.option("--rank", type=int, default=0, help="top N by 24h quote volume")
@click.option("--since", required=True, help="YYYY-MM-DD")
@click.option("--until", required=True, help="YYYY-MM-DD (clamped to seal)")
@click.option(
    "--rebalance", type=int, default=5, show_default=True, help="every N bars"
)
@click.option(
    "--set",
    "sets",
    multiple=True,
    metavar="K=V",
    help="factor param, repeatable (e.g. --set lookback=180 --set hold=20)",
)
@click.option("--note", default="", help="free text into the manifest")
def screen(
    factor: str,
    dataset: str,
    symbols: list[str],
    rank: int,
    since: str,
    until: str,
    rebalance: int,
    sets: tuple[str, ...],
    note: str,
) -> None:
    """run a factor through the screening harness (holdout sealed)"""
    if symbols:
        syms = symbols
    elif rank:
        ranked = universe_mod.rank_by_quote_volume("um", rank)
        syms = [symbol for symbol, _ in ranked]
        click.echo(
            f"universe: top {rank} by 24h quote volume as of today"
            " (a today-fact; the manifest freezes this list)",
            err=True,
        )
    else:
        raise click.UsageError("pass --symbols or --rank N")
    spec = screen_mod.ScreenSpec(
        factor=factor,
        params=_parse_sets(sets),
        dataset=dataset,
        symbols=syms,
        rebalance_every=rebalance,
        start=since,
        end=until,
        note=note,
    )
    wide = screen_mod.signals.load_closes(spec.dataset, syms)
    funding = (
        screen_mod.signals.load_funding(syms)
        if spec.factor in screen_mod.signals.FAST_FACTORS
        else None
    )
    result = screen_mod.run_screen(spec, wide, funding)
    click.echo(f"run_id        {spec.run_id}")
    click.echo(f"sealed before {result.sealed_from} (holdout unread)")
    click.echo(
        f"{'cost_bp':>8} {'sharpe':>7} {'cagr':>7} {'maxDD':>7}"
        f" {'turnover/pa':>11} {'cost/pa':>8} {'days':>5}"
    )
    for cost, m in sorted(result.metrics.items()):
        click.echo(
            f"{cost:>8g} {m['sharpe']:>7.2f} {m.get('cagr', 0):>7.2%}"
            f" {m.get('max_drawdown', 0):>7.2%} {m.get('turnover_pa', 0):>11.1f}"
            f" {m.get('cost_drag_pa', 0):>8.2%} {m['days']:>5}"
        )
    verdict = "PASS" if result.passed_stressed else "fail"
    click.echo(f"stressed-cost verdict: {verdict} (screening only — not proof)")
    manifest = screen_mod.record_run(result)
    try:
        shown = manifest.relative_to(REPO_ROOT)
    except ValueError:  # redirected (tests)
        shown = manifest
    click.echo(f"ledger += rows; manifest -> {shown}")


@cli.group()
def universe() -> None:
    """dated symbol-list snapshots"""


@universe.command()
def sync() -> None:
    """fetch spot + um lists and store them dated"""
    for market in ("spot", "um"):
        snapshot = universe_mod.fetch_snapshot(market)
        path = universe_mod.store_snapshot(snapshot)
        click.echo(f"{market}: {len(snapshot.symbols)} USDT symbols -> " f"{path.name}")


@universe.command()
def show() -> None:
    """what snapshots are on disk"""
    for market in ("spot", "um"):
        stored = universe_mod.latest(market)
        if stored is None:
            click.echo(f"{market}: no snapshot — run `quant universe sync`")
        else:
            click.echo(f"{market}: {len(stored.symbols)} symbols as of {stored.day}")


@universe.command()
@click.option("--market", type=click.Choice(["spot", "um"]), default="um")
@click.option("--top", type=int, default=0, help="rank: how many symbols")
def rank(market: str, top: int) -> None:
    """rank a market by 24h quote volume"""
    ranked = universe_mod.rank_by_quote_volume(market, top)
    click.echo(",".join(symbol for symbol, _ in ranked))
    for symbol, volume in ranked:
        click.echo(f"{symbol:<12} {volume:>.0f}", err=True)


if __name__ == "__main__":
    cli()
