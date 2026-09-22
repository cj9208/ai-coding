"""`orchestrate` — the operator's view of the runtime (M1 surface).

``ask`` is the only command that hits an LLM (via the M1 front half); every
other subcommand stays deterministic: ``status`` / ``replay`` /
``handoff list|export|worklist|claim|resolve`` / ``golden run`` /
``registry check``. The worklist half (05d) is a *consumer* of persisted
packets — no command here touches the state machine or mutates a packet
(DP-3).

Declaration notes (post-migration from argparse, repo-wide click
convention): mirrors ``rag.cli``, so the surface stays
``orchestrate --db PATH COMMAND`` — ``--db`` is a *group* option and the
``Store`` it opens lives on ``ctx.obj``, closed when the invocation unwinds
(the old ``try/finally`` in ``main``). Every token in
``docs/orchestrator/03-usage.md`` is unchanged, and ``golden`` /
``registry`` are real click subgroups now. Failure lines are kept
byte-identical (``click.echo(..., err=True)`` plus exit 1) rather than
routed through ``ClickException``, because that doc's output table quotes
them — ``ask failed: <exc>``, ``unknown request: <id>``. One real
difference: click runs the group callback before the subcommand parses its
own arguments, so ``orchestrate --db X <cmd> --help`` opens (and therefore
creates) the SQLite file, which argparse never did.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import click

from utils.paths import REPO_ROOT

from .capabilities.builtin import render_handoff_markdown
from .config import DB_PATH
from .contracts import HandoffPacket
from .golden import load_cases, run_cases
from .runtime import _now_ms
from .store import Store

DEFAULT_GOLDEN_FILE = REPO_ROOT / "config" / "orchestrator" / "golden_cases.jsonl"


@click.group()
@click.option("--db", default=None, help=f"SQLite path (default: {DB_PATH})")
@click.pass_context
def cli(ctx: click.Context, db: str | None) -> None:
    """enterprise orchestrator runtime (ask + inspect + replay)"""
    store = Store(db)
    ctx.obj = store
    ctx.call_on_close(store.close)


# -- commands ----------------------------------------------------------------
@cli.command()
@click.argument("text")
@click.option(
    "--resume",
    default=None,
    metavar="REQ_ID",
    help="answer a pending clarification instead of starting a request",
)
@click.option("--user", default="cli_user")
@click.option(
    "--locale",
    default="zh",
    help="front-half policy pack (orchestrator.packs; zh|en)",
)
@click.pass_obj
def ask(store: Store, text: str, resume: str | None, user: str, locale: str) -> None:
    """one turn against the real front half (LLM)"""
    from .interpret import LlmFrontHalf
    from .registry import load_default
    from .runtime import Orchestrator

    orchestrator = Orchestrator(store, load_default(), LlmFrontHalf())
    try:
        if resume:
            result = orchestrator.resume(resume, text)
        else:
            result = orchestrator.run_turn(text, user_id=user, locale=locale)
    except Exception as exc:  # front-half failure, or a CAS loss on --resume
        click.echo(f"ask failed: {exc}", err=True)
        raise SystemExit(1)
    if result.question:
        click.echo(f"需要澄清：{result.question}")
        click.echo(
            f'(补充后继续：orchestrate ask --resume {result.request_id} "你的回答")'
        )
    else:
        click.echo(result.response)
    click.echo(f"[{result.status.value}] request_id={result.request_id}")


@cli.command()
@click.argument("request_id", required=False, default=None)
@click.option("--limit", type=int, default=20)
@click.pass_obj
def status(store: Store, request_id: str | None, limit: int) -> None:
    """list recent requests, or one in detail"""
    if not request_id:
        rows = store.list_requests(limit=limit)
        if not rows:
            click.echo("no requests yet")
            return
        for r in rows:
            click.echo(
                f"{r['request_id']}  {r['status']:<24} {r['user_id']}"
                f"  {r['updated_at_ms']}"
            )
        return
    envelope = store.get_request(request_id)
    if envelope is None:
        click.echo(f"unknown request: {request_id}", err=True)
        raise SystemExit(1)
    click.echo(
        json.dumps(envelope.model_dump(mode="json"), indent=2, ensure_ascii=False)
    )


@cli.command()
@click.argument("request_id")
@click.pass_obj
def replay(store: Store, request_id: str) -> None:
    """reconstruct one request's decision path"""
    envelope = store.get_request(request_id)
    if envelope is None:
        click.echo(f"unknown request: {request_id}", err=True)
        raise SystemExit(1)
    click.echo(
        f"request {envelope.request_id}  status={envelope.state.current_status.value}"
        f"  input={envelope.original_input.text!r}"
    )
    from .registry import CAPABILITIES_PATH, config_hash_of

    recorded = envelope.config_hash
    now = config_hash_of(CAPABILITIES_PATH) if CAPABILITIES_PATH.is_file() else None
    config_line = f"config: {recorded or 'unrecorded (pre-05d row or static fixture)'}"
    if recorded is None:
        config_line += f"  (now: {now})"
    elif recorded == now:
        config_line += "  (matches current artifact)"
    else:
        config_line += f"  (DRIFT: current artifact is {now})"
    click.echo(config_line)
    counters = envelope.attempt_counters.model_dump()
    click.echo(f"counters: {counters}\n")
    for obj in store.objects(request_id):
        payload = obj["payload"]
        click.echo(f"[{obj['seq']:>2}] {obj['kind']}: {_one_line(payload)}")
    click.echo()
    for ev in store.events(request_id):
        click.echo(
            f"{ev['created_at_ms']}  {ev['event']:<32} {_one_line(ev['payload'])}"
        )


@cli.group()
def handoff() -> None:
    """inspect handoff packets"""


@handoff.command("list")
@click.pass_obj
def handoff_list(store: Store) -> None:
    """most recent handoffs"""
    rows = store.objects_of_kind("handoff")
    if not rows:
        click.echo("no handoffs")
        return
    for row in rows:
        reason = row["payload"].get("reason", {})
        click.echo(
            f"{row['payload'].get('handoff_id')}  req={row['request_id']}"
            f"  reason={reason.get('code')}"
        )


@handoff.command("worklist")
@click.option(
    "--all", "show_all", is_flag=True, help="include resolved tickets (default: not)"
)
@click.pass_obj
def handoff_worklist(store: Store, show_all: bool) -> None:
    """packets as owned work: open/claimed/resolved"""
    rows = store.objects_of_kind("handoff", limit=200)
    if not rows:
        click.echo("no handoffs")
        return
    shown = 0
    for row in rows:
        hid = str(row["payload"].get("handoff_id"))
        ticket = store.get_ticket(hid)
        status = ticket["ticket_status"] if ticket else "open"
        if status == "resolved" and not show_all:
            continue
        assignee = ticket["assignee"] if ticket else "-"
        click.echo(
            f"{hid}  {status:<8} {assignee:<16} req={row['request_id']}"
            f"  reason={row['payload'].get('reason', {}).get('code')}"
            f"  {row['created_at_ms']}"
        )
        shown += 1
    if not shown:
        click.echo("worklist empty (resolved tickets hidden without --all)")


@handoff.command("claim")
@click.argument("handoff_id")
@click.option("--assignee", required=True)
@click.option(
    "--reassign",
    is_flag=True,
    help="take over a packet someone else claimed",
)
@click.pass_obj
def handoff_claim(store: Store, handoff_id: str, assignee: str, reassign: bool) -> None:
    """take ownership of a handoff packet"""
    try:
        ticket = store.claim_ticket(handoff_id, assignee, _now_ms(), reassign=reassign)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)
    click.echo(f"claimed {ticket['handoff_id']} for {ticket['assignee']}")


@handoff.command("resolve")
@click.argument("handoff_id")
@click.option("--assignee", required=True, help="must be the claimant")
@click.option("--note", required=True)
@click.pass_obj
def handoff_resolve(store: Store, handoff_id: str, assignee: str, note: str) -> None:
    """close a claimed ticket with a note"""
    try:
        ticket = store.resolve_ticket(handoff_id, assignee, note, _now_ms())
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)
    click.echo(f"resolved {ticket['handoff_id']} by {ticket['assignee']}")


@handoff.command("export")
@click.argument("handoff_id")
@click.pass_obj
def handoff_export(store: Store, handoff_id: str) -> None:
    """render the six-section markdown packet"""
    for row in store.objects_of_kind("handoff", limit=200):
        if row["payload"].get("handoff_id") == handoff_id:
            packet = HandoffPacket.model_validate(row["payload"])
            click.echo(render_handoff_markdown(packet))
            return
    click.echo(f"unknown handoff: {handoff_id}", err=True)
    raise SystemExit(1)


@cli.group()
def golden() -> None:
    """run golden regression cases"""


@golden.command("run")
@click.option(
    "--file", default=None, help="JSONL cases (default: config/orchestrator/)"
)
@click.pass_obj
def golden_run(store: Store, file: str | None) -> None:
    """replay cases and diff emitted decisions"""
    del store  # the runner uses its own throwaway DB
    path = Path(file) if file else DEFAULT_GOLDEN_FILE
    if not path.exists():
        click.echo(f"no golden case file at {path}", err=True)
        raise SystemExit(1)
    cases = load_cases(path)
    with tempfile.TemporaryDirectory() as tmp:
        results = run_cases(cases, db_path=Path(tmp) / "golden.db")
    failures = 0
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        click.echo(
            f"[{mark}] {r.case_id}  status={r.status.value}" f"  rows={r.fired_row_ids}"
        )
        for d in r.diffs:
            click.echo(f"       - {d}")
            failures += 1
    click.echo(
        f"\n{len(results) - sum(1 for r in results if not r.ok)}/{len(results)} cases ok"
    )
    raise SystemExit(1 if failures else 0)


@cli.group()
def registry() -> None:
    """inspect the capability registry"""


@registry.command("check")
@click.pass_obj
def registry_check(store: Store) -> None:
    """validate capabilities.yaml against the schema"""
    del store  # schema work only; the group's store is opened for every command
    from .registry import CAPABILITIES_PATH, load_default, load_yaml

    try:
        entries = load_yaml()
    except (ValueError, KeyError) as exc:  # schema failures, not crashes
        click.echo(f"{CAPABILITIES_PATH}: {exc}", err=True)
        raise SystemExit(1)
    reg = load_default()
    for name, entry in entries.items():
        impl = "impl bound" if name in reg.impls else "no impl (escalation/config)"
        click.echo(
            f"{name:<16} v{entry.capability_version:<8} task_types="
            f"{entry.task_types_supported}  {impl}"
        )
    click.echo(
        f"{CAPABILITIES_PATH}: ok, {len(entries)} entries"
        f"  config_hash={reg.config_hash}"
    )


def _one_line(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= 160 else text[:157] + "..."


if __name__ == "__main__":  # pragma: no cover
    cli()
