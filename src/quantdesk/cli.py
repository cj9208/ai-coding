"""``quant`` CLI — the one entry point of the research data plane.

    quant download --datasets um_klines_1m --symbols BTCUSDT \
                   --since 2024-01 --until 2024-12       # diff-sync archives
    quant convert  --datasets um_klines_1m --symbols BTCUSDT \
                   --since 2024-01 --until 2024-12       # raw -> Parquet months
    quant list                                            # coverage table
    quant verify [--remote]                               # hash/grid/replacement
    quant universe sync|show                              # dated symbol lists

download/convert are pure "make local match the requested range"
commands — rerunning them is a no-op, matching the house posture that
provisioning is an idempotent CLI subcommand, not a shell script.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta

from . import archive
from . import convert as convert_mod
from . import record, store, universe
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


def cmd_universe(args: argparse.Namespace) -> int:
    if args.action == "sync":
        for market in ("spot", "um"):
            snapshot = universe.fetch_snapshot(market)
            path = universe.store_snapshot(snapshot)
            print(f"{market}: {len(snapshot.symbols)} USDT symbols -> " f"{path.name}")
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

    p = sub.add_parser("universe", help="dated symbol-list snapshots")
    p.add_argument("action", choices=["sync", "show"])
    p.set_defaults(func=cmd_universe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
