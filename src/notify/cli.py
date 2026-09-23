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

import json
from typing import Any

import click

from . import channels, config, ledger
from .events import emit as emit_event

#: emit's named kwargs — a payload key of the same name is ambiguous, so the
#: CLI rejects it up front instead of letting **payload collide.
_RESERVED_PAYLOAD_KEYS = {"severity", "dedup_key"}


@click.group()
def cli() -> None:
    """append-only event ledger + one-shot dispatcher"""


@cli.command()
@click.argument("project")
@click.argument("kind")
@click.option(
    "--severity",
    type=click.Choice(["info", "warn", "alert"]),
    default="info",
)
@click.option("--dedup-key", default=None, help="default: project:kind")
@click.argument("payload", nargs=-1, metavar="KEY=VALUE")
def emit(
    project: str,
    kind: str,
    severity: str,
    dedup_key: str | None,
    payload: tuple[str, ...],
) -> None:
    """append one event to the ledger"""
    try:
        parsed = dict(_parse_pair(p) for p in payload)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    reserved = _RESERVED_PAYLOAD_KEYS & parsed.keys()
    if reserved:
        flags = ", ".join(f"--{k.replace('_', '-')}" for k in sorted(reserved))
        raise click.ClickException(
            f"payload keys {sorted(reserved)} are reserved" f" — pass them as {flags}"
        )
    event_id = emit_event(
        project,
        kind,
        severity=severity,
        dedup_key=dedup_key,
        **parsed,
    )
    if event_id is None:
        raise SystemExit(1)  # emit already explained itself on stderr
    click.echo(f"{event_id}")


@cli.command()
@click.option("--project", default=None)
@click.option("--recent", type=int, default=0, help="also list N latest events")
def status(project: str | None, recent: int) -> None:
    """ledger summary: counts per kind, last event"""
    led = ledger.default()
    rows = led.summary(project)
    if not rows:
        click.echo("ledger empty")
        return
    width = max(len(f"{p} {k} {s}") for p, k, s, _, _ in rows)
    for row_project, kind, severity, n, last_ts in rows:
        label = f"{row_project} {kind} {severity}"
        click.echo(f"{label:<{width}}  {n:>6}  last {last_ts}")
    click.echo(f"total {led.total()} events")
    if recent:
        click.echo("\nrecent:")
        for ev in led.recent(recent):
            click.echo(f"  #{ev.id} " + channels.render(ev))


@cli.command()
@click.option(
    "--channels",
    "channel_list",
    default=None,
    help="override NOTIFY_CHANNELS (comma list, e.g. stdout,telegram)",
)
@click.option("--limit", type=int, default=500, help="max events per channel")
def dispatch(channel_list: str | None, limit: int) -> None:
    """deliver pending events to enabled channels"""
    names = (
        [c.strip() for c in channel_list.split(",") if c.strip()]
        if channel_list
        else config.enabled_channels()
    )
    led = ledger.default()
    for name in names:
        try:
            channel = channels.build(name)
        except channels.ChannelError as exc:
            raise click.ClickException(f"notify: {exc}") from exc
        sent = failed = 0
        for event in led.pending_for(name, limit=limit):
            try:
                channel.send(event)
                led.mark_delivery(event.id, name, "sent")
                sent += 1
            except Exception as exc:  # noqa: BLE001 — recorded, not fatal
                led.mark_delivery(event.id, name, "failed", str(exc))
                failed += 1
        click.echo(f"{name}: sent {sent}, failed {failed}")
    # failed > 0 still exits 0: escalating it into a delivery.failed alert
    # is policy.py's M1 job, not this wiring's.


def _parse_pair(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise ValueError(f"payload must be key=value, got '{raw}'")
    key, _, value = raw.partition("=")
    try:
        return key, json.loads(value)
    except json.JSONDecodeError:
        return key, value


if __name__ == "__main__":
    cli()
