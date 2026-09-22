"""``notify`` CLI — the ledger's human face and the dispatcher's cron hook.

    notify emit quantdesk record.batch --severity info rows=240 stream=x
    notify status [--project P] [--recent 20]
    notify dispatch [--channels stdout,telegram]

``dispatch`` is a short-lived process a scheduler launches every few minutes
(D-5): it reads events not yet sent to each enabled channel, delivers, and
marks the outcome. M0 has no throttling yet (policy.py is M1) — everything
goes straight to the channels, alert-first. Delivery *failures* still exit 0
by design: escalating them into an alert is M1's job; a non-zero exit here
means the invocation itself was wrong (unknown channel, bad payload).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import channels, config, ledger
from .events import emit

#: emit's named kwargs — a payload key of the same name is ambiguous, so the
#: CLI rejects it up front instead of letting **payload collide.
_RESERVED_PAYLOAD_KEYS = {"severity", "dedup_key"}


def cmd_emit(args: argparse.Namespace) -> int:
    try:
        payload = dict(_parse_pair(p) for p in args.payload)
    except ValueError as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 1
    reserved = _RESERVED_PAYLOAD_KEYS & payload.keys()
    if reserved:
        flags = ", ".join(f"--{k.replace('_', '-')}" for k in sorted(reserved))
        print(
            f"notify: payload keys {sorted(reserved)} are reserved"
            f" — pass them as {flags}",
            file=sys.stderr,
        )
        return 1
    event_id = emit(
        args.project,
        args.kind,
        severity=args.severity,
        dedup_key=args.dedup_key,
        **payload,
    )
    if event_id is None:
        return 1  # emit already explained itself on stderr
    print(f"{event_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    led = ledger.default()
    rows = led.summary(args.project)
    if not rows:
        print("ledger empty")
        return 0
    width = max(len(f"{p} {k} {s}") for p, k, s, _, _ in rows)
    for project, kind, severity, n, last_ts in rows:
        label = f"{project} {kind} {severity}"
        print(f"{label:<{width}}  {n:>6}  last {last_ts}")
    print(f"total {led.total()} events")
    if args.recent:
        print("\nrecent:")
        for ev in led.recent(args.recent):
            print(f"  #{ev.id} " + channels.render(ev))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    names = (
        [c.strip() for c in args.channels.split(",") if c.strip()]
        if args.channels
        else config.enabled_channels()
    )
    led = ledger.default()
    exit_code = 0
    for name in names:
        try:
            channel = channels.build(name)
        except channels.ChannelError as exc:
            print(f"notify: {exc}", file=sys.stderr)
            return 1
        sent = failed = 0
        for event in led.pending_for(name, limit=args.limit):
            try:
                channel.send(event)
                led.mark_delivery(event.id, name, "sent")
                sent += 1
            except Exception as exc:  # noqa: BLE001 — recorded, not fatal
                led.mark_delivery(event.id, name, "failed", str(exc))
                failed += 1
        print(f"{name}: sent {sent}, failed {failed}")
    # failed > 0 still exits 0: escalating it into a delivery.failed alert
    # is policy.py's M1 job, not this wiring's.
    return exit_code


def _parse_pair(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise ValueError(f"payload must be key=value, got '{raw}'")
    key, _, value = raw.partition("=")
    try:
        return key, json.loads(value)
    except json.JSONDecodeError:
        return key, value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notify", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("emit", help="append one event to the ledger")
    p.add_argument("project", help="emitting package, e.g. quantdesk")
    p.add_argument("kind", help="dotted event name, e.g. record.batch")
    p.add_argument("--severity", choices=["info", "warn", "alert"], default="info")
    p.add_argument("--dedup-key", default=None, help="default: project:kind")
    p.add_argument("payload", nargs="*", metavar="KEY=VALUE", help="JSON-typed values")
    p.set_defaults(func=cmd_emit)

    p = sub.add_parser("status", help="ledger summary: counts per kind, last event")
    p.add_argument("--project", default=None)
    p.add_argument("--recent", type=int, default=0, help="also list N latest events")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("dispatch", help="deliver pending events to enabled channels")
    p.add_argument(
        "--channels",
        default=None,
        help="override NOTIFY_CHANNELS (comma list, e.g. stdout,telegram)",
    )
    p.add_argument("--limit", type=int, default=500, help="max events per channel")
    p.set_defaults(func=cmd_dispatch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
