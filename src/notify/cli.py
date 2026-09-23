"""``notify`` CLI — the ledger's human face and the dispatcher's cron hook.

    notify emit quantdesk record.batch --severity info rows=240 stream=x
    notify status [--project P] [--recent 20]
    notify dispatch [--channels stdout,telegram] [--limit 500]
    notify check [--send]
    notify schedule install [--interval-minutes 5] | status | run-now | remove

``dispatch`` is the short-lived process a scheduler launches every few minutes
(§1: no daemon), and one invocation walks §4 in order: rules turn a silence into an
alert on the ledger (§4.5), policy decides per channel what actually goes out
(§4.4), delivery marks the outcome, and whatever could not be delivered
becomes a self-alert for the next round. Delivery *failures* still exit 0 —
the failure is now a ledger row, which is the layer's whole point; a non-zero
exit means the invocation itself was wrong (unknown channel, bad payload,
unreadable rules file).

``schedule`` wraps what makes that launcher correct (§4.9) so nobody retypes it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import click

from . import channels, config, ledger, policy, rules
from . import schedule as sched
from .events import Event, Severity
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
    """run the rules, then deliver pending alerts to each channel"""
    names = (
        [c.strip() for c in channel_list.split(",") if c.strip()]
        if channel_list
        else config.enabled_channels()
    )
    led = ledger.default()
    _run_rules(led)

    known: dict[int, Event] = {}
    failed_on: dict[int, set[str]] = {}
    last_error: dict[int, str] = {}
    stuck: dict[int, Event] = {}

    for name in names:
        try:
            channel = channels.build(name)
        except channels.ChannelError as exc:
            raise click.ClickException(f"notify: {exc}") from exc
        pending = led.pending_for(name, limit=limit)
        for event in pending:
            known[event.id] = event
        decision = policy.plan(
            pending,
            last_sent=led.last_sent_by_key(name),
            attempts=led.attempts(name, [e.id for e in pending]),
        )
        sent = failed = 0
        for item in decision.deliveries:
            try:
                channel.send(item.event)
            except Exception as exc:  # noqa: BLE001 — recorded, not fatal
                for event_id in item.folded:
                    led.mark_delivery(event_id, name, "failed", str(exc))
                    failed_on.setdefault(event_id, set()).add(name)
                    last_error[event_id] = str(exc)
                    failed += 1
            else:
                note = (
                    ""
                    if item.reason == "first"
                    else f"merged {len(item.folded)} events into one notice"
                )
                for event_id in item.folded:
                    led.mark_delivery(event_id, name, "sent", note)
                    sent += 1
        for event in decision.stuck:
            stuck[event.id] = event
        click.echo(
            f"{name}: messages {len(decision.deliveries)}, sent {sent}, failed {failed}"
        )

    _escalate(led, names, known, failed_on, last_error, stuck)


@cli.command()
@click.option(
    "--send",
    "do_send",
    is_flag=True,
    help="fire one live probe message through every enabled channel",
)
def check(do_send: bool) -> None:
    """validate config, dry-run the rules, optionally probe the wire"""
    led = ledger.default()
    click.echo(f"ledger    {config.db_path()} ({led.total()} events)")
    names = config.enabled_channels()
    click.echo(f"channels  {', '.join(names) if names else '(none enabled)'}")
    for name in names:
        if name not in channels.names():
            raise click.ClickException(
                f"unknown channel '{name}' (available: {', '.join(channels.names())})"
            )

    path = config.expectations_path()
    try:
        expectations = rules.load(path)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"notify: {exc}") from exc
    click.echo(f"rules     {path} ({len(expectations)} declared)")
    for rule in expectations:
        state = "on " if rule.enabled else "off"
        click.echo(f"  [{state}] {rule.project}.{rule.kind} > {rule.max_silence}")
    due = rules.evaluate(expectations, led.last_seen(), led.last_event_ts_by_key())
    click.echo(
        f"  firing now: {len(due)}" + ("" if due else " (dry run, nothing emitted)")
    )
    for event in due:
        click.echo("    " + channels.render(event))

    if not do_send:
        return
    probe = Event.new(
        "notify", "check", severity=Severity.WARN, payload={"probe": True}
    )
    for name in names:
        try:
            channel = channels.build(name)
            channel.send(probe)
        except Exception as exc:  # noqa: BLE001 — a probe reports, never aborts
            click.echo(f"probe {name}: FAILED — {exc}")
        else:
            click.echo(f"probe {name}: ok")


@cli.group(name="schedule")
def schedule_cmd() -> None:
    """register the recurring `dispatch` round (Task Scheduler, §4.9)"""


@schedule_cmd.command(name="install")
@click.option(
    "--interval-minutes",
    type=click.IntRange(min=1),
    default=sched.DEFAULT_INTERVAL_MINUTES,
    show_default=True,
    help="how often dispatch runs; also bounds alert latency",
)
def schedule_install(interval_minutes: int) -> None:
    """register (or update) the task — the scheduler, not a human, runs dispatch"""
    try:
        sched.install(interval_minutes)
    except sched.ScheduleError as exc:
        raise click.ClickException(f"notify: {exc}") from exc
    click.echo(
        f"registered '{sched.TASK_NAME}' every {interval_minutes} min"
        f" — check with `notify schedule status`, log at {sched.LOG_PATH}"
    )


@schedule_cmd.command(name="remove")
def schedule_remove() -> None:
    """unregister the task"""
    try:
        sched.remove()
    except sched.ScheduleError as exc:
        raise click.ClickException(f"notify: {exc}") from exc
    click.echo(f"unregistered '{sched.TASK_NAME}'")


@schedule_cmd.command(name="run-now")
def schedule_run_now() -> None:
    """fire one round through the scheduler — proves the launch, not just dispatch"""
    try:
        sched.run_now()
    except sched.ScheduleError as exc:
        raise click.ClickException(f"notify: {exc}") from exc
    click.echo(
        f"started '{sched.TASK_NAME}' — give it a few seconds,"
        " then `notify schedule status`"
    )


@schedule_cmd.command(name="status")
@click.option("--log-lines", type=int, default=10, show_default=True)
def schedule_status(log_lines: int) -> None:
    """the scheduler's view of the task, plus the tail of its dispatch log"""
    try:
        click.echo(sched.status(log_lines=log_lines))
    except sched.ScheduleError as exc:
        raise click.ClickException(f"notify: {exc}") from exc


def _run_rules(led: ledger.Ledger) -> None:
    """§4.5: a silence becomes an alert here, emitted by notify itself."""
    path = config.expectations_path()
    try:
        expectations = rules.load(path)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"notify: cannot use {path}: {exc}") from exc
    if not expectations:
        return
    due = rules.evaluate(expectations, led.last_seen(), led.last_event_ts_by_key())
    for event in due:
        led.insert(event)
    if due:
        keys = ", ".join(e.dedup_key for e in due)
        click.echo(f"rules: emitted {len(due)} silence alert(s): {keys}")


def _escalate(
    led: ledger.Ledger,
    names: list[str],
    known: dict[int, Event],
    failed_on: dict[int, set[str]],
    last_error: dict[int, str],
    stuck: dict[int, Event],
) -> None:
    """§4.4's bootstrap exception: notify alerting on its own dead channel.

    Two triggers — an event that failed on *every* enabled channel this round,
    or one that has failed ``MAX_ATTEMPTS`` rounds running. The resulting
    self-alert is emitted at the *end* of dispatch, so it goes out on the next
    round over all channels, and it is throttled like any other dedup_key so a
    channel that stays down for a day is one message an hour, not 288.
    """
    every_channel = set(names)
    hopeless = {
        event_id
        for event_id, failed_channels in failed_on.items()
        if every_channel and failed_channels >= every_channel
    }
    offenders = [
        known[event_id]
        for event_id in sorted(hopeless | set(stuck))
        if event_id in known
    ]
    offenders = [
        e
        for e in offenders
        if e.severity in policy.ESCALATE_SEVERITIES and not policy.is_self_alert(e)
    ]
    if not offenders:
        return

    already = led.last_event_ts_by_key().get(policy.SELF_DEDUP_KEY)
    now = datetime.now(timezone.utc)
    if policy.within_window(already, now, policy.THROTTLE_WINDOW):
        click.echo(
            f"escalation throttled: {len(offenders)} undeliverable event(s)"
            " already reported this window"
        )
        return

    ids = [e.id for e in offenders]
    emit_event(
        policy.SELF_PROJECT,
        policy.SELF_KIND,
        severity=Severity.ALERT,
        dedup_key=policy.SELF_DEDUP_KEY,
        undeliverable=len(offenders),
        event_ids=ids,
        channels=list(names),
        last_error=last_error.get(ids[0], "")[:200],
    )
    click.echo(f"escalated: {policy.SELF_DEDUP_KEY} for event(s) {ids}")


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
