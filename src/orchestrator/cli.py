"""`orchestrate` — the operator's view of the runtime (M1 surface).

``ask`` is the only command that hits an LLM (via the M1 front half); every
other subcommand stays deterministic: ``status`` / ``replay`` /
``handoff list|export|worklist|claim|resolve`` / ``golden run``. The
worklist half (05d) is a *consumer* of persisted packets — no command
here touches the state machine or mutates a packet (DP-3). Mirrors
``rag.cli``: argparse subcommands, ``main(argv) -> int``, no
module-level side effects.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from utils.paths import REPO_ROOT

from .capabilities.builtin import render_handoff_markdown
from .config import DB_PATH
from .contracts import HandoffPacket
from .golden import load_cases, run_cases
from .runtime import _now_ms
from .store import Store

DEFAULT_GOLDEN_FILE = REPO_ROOT / "config" / "orchestrator" / "golden_cases.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrate",
        description="enterprise orchestrator runtime (ask + inspect + replay)",
    )
    parser.add_argument("--db", default=None, help=f"SQLite path (default: {DB_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="one turn against the real front half (LLM)")
    p.add_argument("text", help="the request, in the user's own words")
    p.add_argument(
        "--resume",
        default=None,
        metavar="REQ_ID",
        help="answer a pending clarification instead of starting a request",
    )
    p.add_argument("--user", default="cli_user")
    p.add_argument(
        "--locale",
        default="zh",
        help="front-half policy pack (orchestrator.packs; zh|en)",
    )
    p.set_defaults(func=_cmd_ask)
    p = sub.add_parser("status", help="list recent requests, or one in detail")
    p.add_argument("request_id", nargs="?")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("replay", help="reconstruct one request's decision path")
    p.add_argument("request_id")
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser("handoff", help="inspect handoff packets")
    hs = p.add_subparsers(dest="handoff_command", required=True)
    hs.add_parser("list", help="most recent handoffs").set_defaults(
        func=_cmd_handoff_list
    )
    hx = hs.add_parser("export", help="render the six-section markdown packet")
    hx.add_argument("handoff_id")
    hx.set_defaults(func=_cmd_handoff_export)
    wl = hs.add_parser("worklist", help="packets as owned work: open/claimed/resolved")
    wl.add_argument(
        "--all", action="store_true", help="include resolved tickets (default: not)"
    )
    wl.set_defaults(func=_cmd_handoff_worklist)
    cl = hs.add_parser("claim", help="take ownership of a handoff packet")
    cl.add_argument("handoff_id")
    cl.add_argument("--assignee", required=True)
    cl.add_argument(
        "--reassign",
        action="store_true",
        help="take over a packet someone else claimed",
    )
    cl.set_defaults(func=_cmd_handoff_claim)
    rsv = hs.add_parser("resolve", help="close a claimed ticket with a note")
    rsv.add_argument("handoff_id")
    rsv.add_argument("--assignee", required=True, help="must be the claimant")
    rsv.add_argument("--note", required=True)
    rsv.set_defaults(func=_cmd_handoff_resolve)

    p = sub.add_parser("golden", help="run golden regression cases")
    gs = p.add_subparsers(dest="golden_command", required=True)
    gr = gs.add_parser("run", help="replay cases and diff emitted decisions")
    gr.add_argument(
        "--file", default=None, help="JSONL cases (default: config/orchestrator/)"
    )
    gr.set_defaults(func=_cmd_golden_run)

    p = sub.add_parser("registry", help="inspect the capability registry")
    rs = p.add_subparsers(dest="registry_command", required=True)
    rc = rs.add_parser("check", help="validate capabilities.yaml against the schema")
    rc.set_defaults(func=_cmd_registry_check)
    return parser


# -- commands ----------------------------------------------------------------
def _cmd_ask(store: Store, args: argparse.Namespace) -> int:
    from .interpret import LlmFrontHalf
    from .registry import load_default
    from .runtime import Orchestrator

    orchestrator = Orchestrator(store, load_default(), LlmFrontHalf())
    try:
        if args.resume:
            result = orchestrator.resume(args.resume, args.text)
        else:
            result = orchestrator.run_turn(
                args.text, user_id=args.user, locale=args.locale
            )
    except Exception as exc:  # front-half failure, or a CAS loss on --resume
        print(f"ask failed: {exc}", file=sys.stderr)
        return 1
    if result.question:
        print(f"需要澄清：{result.question}")
        print(f'(补充后继续：orchestrate ask --resume {result.request_id} "你的回答")')
    else:
        print(result.response)
    print(f"[{result.status.value}] request_id={result.request_id}")
    return 0


def _cmd_registry_check(store: Store, args: argparse.Namespace) -> int:
    del store, args
    from .registry import CAPABILITIES_PATH, load_default, load_yaml

    try:
        entries = load_yaml()
    except (ValueError, KeyError) as exc:  # schema failures, not crashes
        print(f"{CAPABILITIES_PATH}: {exc}", file=sys.stderr)
        return 1
    registry = load_default()
    for name, entry in entries.items():
        impl = "impl bound" if name in registry.impls else "no impl (escalation/config)"
        print(
            f"{name:<16} v{entry.capability_version:<8} task_types="
            f"{entry.task_types_supported}  {impl}"
        )
    print(f"{CAPABILITIES_PATH}: ok, {len(entries)} entries")
    return 0


def _cmd_status(store: Store, args: argparse.Namespace) -> int:
    if not args.request_id:
        rows = store.list_requests(limit=args.limit)
        if not rows:
            print("no requests yet")
            return 0
        for r in rows:
            print(
                f"{r['request_id']}  {r['status']:<24} {r['user_id']}"
                f"  {r['updated_at_ms']}"
            )
        return 0
    envelope = store.get_request(args.request_id)
    if envelope is None:
        print(f"unknown request: {args.request_id}", file=sys.stderr)
        return 1
    print(json.dumps(envelope.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0


def _cmd_replay(store: Store, args: argparse.Namespace) -> int:
    envelope = store.get_request(args.request_id)
    if envelope is None:
        print(f"unknown request: {args.request_id}", file=sys.stderr)
        return 1
    print(
        f"request {envelope.request_id}  status={envelope.state.current_status.value}"
        f"  input={envelope.original_input.text!r}"
    )
    counters = envelope.attempt_counters.model_dump()
    print(f"counters: {counters}\n")
    for obj in store.objects(args.request_id):
        payload = obj["payload"]
        print(f"[{obj['seq']:>2}] {obj['kind']}: {_one_line(payload)}")
    print()
    for ev in store.events(args.request_id):
        print(f"{ev['created_at_ms']}  {ev['event']:<32} {_one_line(ev['payload'])}")
    return 0


def _cmd_handoff_list(store: Store, args: argparse.Namespace) -> int:
    rows = store.objects_of_kind("handoff")
    if not rows:
        print("no handoffs")
        return 0
    for row in rows:
        reason = row["payload"].get("reason", {})
        print(
            f"{row['payload'].get('handoff_id')}  req={row['request_id']}"
            f"  reason={reason.get('code')}"
        )
    return 0


def _cmd_handoff_worklist(store: Store, args: argparse.Namespace) -> int:
    rows = store.objects_of_kind("handoff", limit=200)
    if not rows:
        print("no handoffs")
        return 0
    shown = 0
    for row in rows:
        hid = str(row["payload"].get("handoff_id"))
        ticket = store.get_ticket(hid)
        status = ticket["ticket_status"] if ticket else "open"
        if status == "resolved" and not args.all:
            continue
        assignee = ticket["assignee"] if ticket else "-"
        print(
            f"{hid}  {status:<8} {assignee:<16} req={row['request_id']}"
            f"  reason={row['payload'].get('reason', {}).get('code')}"
            f"  {row['created_at_ms']}"
        )
        shown += 1
    if not shown:
        print("worklist empty (resolved tickets hidden without --all)")
    return 0


def _cmd_handoff_claim(store: Store, args: argparse.Namespace) -> int:
    try:
        ticket = store.claim_ticket(
            args.handoff_id, args.assignee, _now_ms(), reassign=args.reassign
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"claimed {ticket['handoff_id']} for {ticket['assignee']}")
    return 0


def _cmd_handoff_resolve(store: Store, args: argparse.Namespace) -> int:
    try:
        ticket = store.resolve_ticket(
            args.handoff_id, args.assignee, args.note, _now_ms()
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"resolved {ticket['handoff_id']} by {ticket['assignee']}")
    return 0


def _cmd_handoff_export(store: Store, args: argparse.Namespace) -> int:
    for row in store.objects_of_kind("handoff", limit=200):
        if row["payload"].get("handoff_id") == args.handoff_id:
            packet = HandoffPacket.model_validate(row["payload"])
            print(render_handoff_markdown(packet))
            return 0
    print(f"unknown handoff: {args.handoff_id}", file=sys.stderr)
    return 1


def _cmd_golden_run(store: Store, args: argparse.Namespace) -> int:
    del store  # the runner uses its own throwaway DB
    path = Path(args.file) if args.file else DEFAULT_GOLDEN_FILE
    if not path.exists():
        print(f"no golden case file at {path}", file=sys.stderr)
        return 1
    cases = load_cases(path)
    with tempfile.TemporaryDirectory() as tmp:
        results = run_cases(cases, db_path=Path(tmp) / "golden.db")
    failures = 0
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        print(
            f"[{mark}] {r.case_id}  status={r.status.value}" f"  rows={r.fired_row_ids}"
        )
        for d in r.diffs:
            print(f"       - {d}")
            failures += 1
    print(
        f"\n{len(results) - sum(1 for r in results if not r.ok)}/{len(results)} cases ok"
    )
    return 1 if failures else 0


def _one_line(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= 160 else text[:157] + "..."


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
