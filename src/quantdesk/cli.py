"""``quant`` CLI — the one entry point of the research data plane.

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
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta

from utils.paths import REPO_ROOT

from . import archive
from . import convert as convert_mod
from . import record
from . import screen as screen_mod
from . import store, universe
from .config import DATASETS, parse_csv_list


def _default_until() -> str:
    last_month = date.today().replace(day=1) - timedelta(days=1)
    return last_month.strftime("%Y-%m")


def _resolve_datasets(spec: str) -> list[str]:
    names = parse_csv_list(spec)
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        raise SystemExit(
            f"unknown dataset(s): {', '.join(unknown)}; "
            f"available: {', '.join(sorted(DATASETS))}"
        )
    return names


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return parse_csv_list(args.symbols)
    if args.top:
        snap = universe.latest(args.market)
        if snap is None:
            raise SystemExit("no universe snapshot — run `quant universe sync`")
        return snap.symbols[: args.top]
    raise SystemExit("pass --symbols or --top N")


def cmd_download(args: argparse.Namespace) -> int:
    symbols = _resolve_symbols(args)
    counts = {"fetched": 0, "skipped": 0, "replaced": 0, "missing": 0}
    for name in _resolve_datasets(args.datasets):
        actions = archive.download(
            DATASETS[name],
            symbols,
            args.since,
            args.until or _default_until(),
            include_daily=not args.no_daily,
            refresh_remote=args.refresh,
        )
        for action in actions:
            counts[action.outcome] = counts.get(action.outcome, 0) + 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"download: {summary}")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    symbols = _resolve_symbols(args)
    until = args.until or _default_until()
    built = 0
    for name in _resolve_datasets(args.datasets):
        dataset = DATASETS[name]
        for symbol in symbols:
            for month in archive.months_between(args.since, until):
                result = convert_mod.convert_month(dataset, symbol, month)
                if result:
                    built += 1
                    print(
                        f"built {result.dataset}/{result.symbol}/{result.month}"
                        f" rows={result.rows} from {result.source}"
                    )
    print(f"convert: {built} month-partitions (re)built")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    rows = store.scan_coverage()
    if not rows:
        print("no parquet partitions yet — run quant download + quant convert")
        return 0
    print(f"{'dataset':<16} {'symbol':<12} {'months':<18} {'count':>5} rows")
    for row in rows:
        print(
            f"{row.dataset:<16} {row.symbol:<12} {row.span:<18} "
            f"{len(row.months):>5} {row.rows}"
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    issues = store.verify(remote=args.remote)
    for issue in issues:
        print(f"{issue.kind:<11} {issue.subject}: {issue.detail}", file=sys.stderr)
    print(
        f"verify: {len(issues)} issue(s)"
        + (", --remote checked" if args.remote else "")
    )
    return 1 if issues else 0


def cmd_record(args: argparse.Namespace) -> int:
    streams = record.parse_streams(args.streams)
    if args.symbols:
        symbols = parse_csv_list(args.symbols)
    elif args.top:
        snap = universe.latest("um")
        if snap is None:
            raise SystemExit("no universe snapshot — run `quant universe sync`")
        symbols = snap.symbols[: args.top]
    else:
        symbols = []
    if "open_interest" in streams and not symbols:
        raise SystemExit("open_interest needs --symbols or --top (per-symbol poll)")
    state = record.RecorderState(
        symbols=symbols,
        streams=streams,
        minutes=args.minutes,
        silence_alert=args.silence_alert,
    )
    print(
        f"recording {','.join(streams)}"
        + (f" for {len(symbols)} symbols" if symbols else " (all-market flows)")
    )
    try:
        asyncio.run(record.run(state))
    except KeyboardInterrupt:
        print("^C — final flushed", file=sys.stderr)
    return 0


def _parse_sets(pairs: list[str]) -> dict:
    params: dict = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if raw.isdigit():
            params[key.strip()] = int(raw)
        else:
            params[key.strip()] = float(raw)
    return params


def cmd_screen(args: argparse.Namespace) -> int:
    if args.symbols:
        symbols = parse_csv_list(args.symbols)
    elif args.rank:
        ranked = universe.rank_by_quote_volume("um", args.rank)
        symbols = [symbol for symbol, _ in ranked]
        print(
            f"universe: top {args.rank} by 24h quote volume as of today"
            " (a today-fact; the manifest freezes this list)",
            file=sys.stderr,
        )
    else:
        raise SystemExit("pass --symbols or --rank N")
    spec = screen_mod.ScreenSpec(
        factor=args.factor,
        params=_parse_sets(args.set),
        dataset=args.dataset,
        symbols=symbols,
        rebalance_every=args.rebalance,
        start=args.since,
        end=args.until,
        note=args.note,
    )
    wide = screen_mod.signals.load_closes(spec.dataset, symbols)
    funding = (
        screen_mod.signals.load_funding(symbols)
        if spec.factor in screen_mod.signals.FAST_FACTORS
        else None
    )
    result = screen_mod.run_screen(spec, wide, funding)
    print(f"run_id        {spec.run_id}")
    print(f"sealed before {result.sealed_from} (holdout unread)")
    print(
        f"{'cost_bp':>8} {'sharpe':>7} {'cagr':>7} {'maxDD':>7}"
        f" {'turnover/pa':>11} {'cost/pa':>8} {'days':>5}"
    )
    for cost, m in sorted(result.metrics.items()):
        print(
            f"{cost:>8g} {m['sharpe']:>7.2f} {m.get('cagr', 0):>7.2%}"
            f" {m.get('max_drawdown', 0):>7.2%} {m.get('turnover_pa', 0):>11.1f}"
            f" {m.get('cost_drag_pa', 0):>8.2%} {m['days']:>5}"
        )
    verdict = "PASS" if result.passed_stressed else "fail"
    print(f"stressed-cost verdict: {verdict} (screening only — not proof)")
    manifest = screen_mod.record_run(result)
    try:
        shown = manifest.relative_to(REPO_ROOT)
    except ValueError:  # redirected (tests)
        shown = manifest
    print(f"ledger += rows; manifest -> {shown}")
    return 0


def cmd_universe(args: argparse.Namespace) -> int:
    if args.action == "sync":
        for market in ("spot", "um"):
            snapshot = universe.fetch_snapshot(market)
            path = universe.store_snapshot(snapshot)
            print(f"{market}: {len(snapshot.symbols)} USDT symbols -> " f"{path.name}")
        return 0
    if args.action == "rank":
        ranked = universe.rank_by_quote_volume(args.market, args.top)
        print(",".join(symbol for symbol, _ in ranked))
        for symbol, volume in ranked:
            print(f"{symbol:<12} {volume:>.0f}", file=sys.stderr)
        return 0
    for market in ("spot", "um"):
        stored = universe.latest(market)
        if stored is None:
            print(f"{market}: no snapshot — run `quant universe sync`")
        else:
            print(f"{market}: {len(stored.symbols)} symbols as of {stored.day}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quant",
        description="research data plane over the Binance public archive",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_range(p: argparse.ArgumentParser) -> None:
        p.add_argument("--datasets", required=True, help="csv of dataset names")
        p.add_argument("--symbols", default="", help="csv of symbols")
        p.add_argument("--market", default="um", choices=["spot", "um"])
        p.add_argument(
            "--top",
            type=int,
            default=0,
            help="first N from the "
            "latest universe snapshot (requires quant universe sync)",
        )
        p.add_argument("--since", required=True, help="YYYY-MM")
        p.add_argument("--until", default="", help="YYYY-MM (default: last month)")

    p = sub.add_parser("download", help="diff-sync archives into raw/ + inventory")
    add_range(p)
    p.add_argument(
        "--no-daily",
        action="store_true",
        help="skip daily files for the not-yet-published current month",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="re-check upstream checksums and act on replacements",
    )
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("convert", help="build Parquet month partitions from raw")
    add_range(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("list", help="coverage of built partitions")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "verify",
        help="local hashes, time-grid continuity" " [, upstream replacement detector]",
    )
    p.add_argument(
        "--remote",
        action="store_true",
        help="also re-check every inventory file's upstream checksum",
    )
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("record", help="accumulate liquidation/funding/OI feeds")
    p.add_argument("--streams", default="liquidations,funding,open_interest")
    p.add_argument("--symbols", default="", help="csv (per-symbol polls)")
    p.add_argument(
        "--top",
        type=int,
        default=0,
        help="first N from the latest " "um universe snapshot",
    )
    p.add_argument(
        "--minutes",
        type=float,
        default=0.0,
        help="stop after N minutes (default: run until ^C)",
    )
    p.add_argument(
        "--silence-alert",
        type=float,
        default=300.0,
        help="log a gap when the liquidations stream pushes nothing for N seconds",
    )
    p.set_defaults(func=cmd_record)

    p = sub.add_parser(
        "screen",
        help="run a factor through the screening harness (holdout sealed)",
    )
    p.add_argument("--factor", required=True, choices=sorted(screen_mod.FACTORS))
    p.add_argument("--dataset", default="um_klines_1d")
    p.add_argument("--symbols", default="", help="csv of symbols")
    p.add_argument("--rank", type=int, default=0, help="top N by 24h quote volume")
    p.add_argument("--since", required=True, help="YYYY-MM-DD")
    p.add_argument("--until", required=True, help="YYYY-MM-DD (clamped to seal)")
    p.add_argument("--rebalance", type=int, default=5, help="every N bars")
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="K=V",
        help="factor param, repeatable (e.g. --set lookback=180 --set hold=20)",
    )
    p.add_argument("--note", default="", help="free text into the manifest")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("universe", help="dated symbol-list snapshots")
    p.add_argument("action", choices=["sync", "show", "rank"])
    p.add_argument("--market", default="um", choices=["spot", "um"])
    p.add_argument("--top", type=int, default=0, help="rank: how many symbols")
    p.set_defaults(func=cmd_universe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
