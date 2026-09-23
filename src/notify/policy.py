"""Delivery policy: alert jumps the queue, info batches, repeats converge (§4.4).

:func:`plan` is a pure function — every ledger read happens in the caller and
comes in as an argument, so each rule is a golden-file case
(`tests/golden/notify_policy.jsonl`) instead of a database fixture. Changing a
rule without updating the goldens is the failure mode this shape exists to
prevent.

Two §4.4 rules are deliberately *not* here:

- "info waits for the digest" is a query-side filter (``ledger.pending_for``
  asks for alert+warn only), not a plan decision — an event policy never sees
  cannot be mis-planned;
- "failed on **all** configured channels" is cross-channel, so dispatch owns
  it; policy only reports the per-channel half (``Plan.stuck``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .events import Event, Severity

#: repeats of one dedup_key inside this window collapse into a single notice
THROTTLE_WINDOW = timedelta(hours=1)

#: failed rounds on one channel after which an event is reported as stuck
MAX_ATTEMPTS = 3

#: what may escalate. §4.4 names alert; warn rides along because a warn that
#: cannot reach any channel for three rounds is the same kind of news. Info is
#: absent by construction — it waits for the digest and never reaches policy.
ESCALATE_SEVERITIES = (Severity.ALERT, Severity.WARN)

#: the bootstrap exception of §4.4: notify alerting on itself. It must never
#: escalate, or one dead channel amplifies into an alert storm about alerts.
SELF_PROJECT = "notify"
SELF_KIND = "delivery.failed"
SELF_DEDUP_KEY = f"{SELF_PROJECT}:{SELF_KIND}"


def is_self_alert(event: Event) -> bool:
    """A delivery.failed alert must never escalate into another one."""
    return event.project == SELF_PROJECT and event.kind == SELF_KIND


@dataclass(frozen=True)
class Delivery:
    """One message for one channel.

    ``event`` is either a ledger event or a synthetic merge notice (id -1, no
    row of its own); ``folded`` lists the real event ids this delivery
    settles, which is what dispatch marks ``sent``.
    """

    event: Event
    folded: tuple[int, ...] = ()
    #: ``first`` | ``repeat`` — why this message exists, for the audit trail
    reason: str = "first"


@dataclass(frozen=True)
class Plan:
    deliveries: tuple[Delivery, ...] = ()
    #: alert/warn that keep failing on this channel (§4.4's "pending 超过 3 轮")
    stuck: tuple[Event, ...] = ()


def merge_notice(group: Sequence[Event], now: datetime) -> Event:
    """The synthetic "same cause, N times" event for a throttled group.

    Payload stays machine-readable (§4.1): the renderer turns these fields
    into wording, so the notice carries counts and timestamps, not a sentence.
    """
    first, last = group[0], group[-1]
    return Event(
        id=-1,
        ts=now.isoformat(timespec="seconds"),
        project=first.project,
        kind=first.kind,
        severity=first.severity,
        dedup_key=first.dedup_key,
        payload={
            "repeats": len(group),
            "first_ts": first.ts,
            "last_ts": last.ts,
            "event_ids": [e.id for e in group],
            "detail": dict(last.payload),
        },
    )


def parse_ts(raw: str | None) -> datetime | None:
    """Ledger timestamps are ISO-8601 UTC; a naive or malformed one is treated
    as unknown rather than raising inside a dispatch round."""
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def fresh(last_at: str | None, now: datetime, window: timedelta) -> bool:
    """True when this dedup_key owes an unconditional immediate delivery:
    never sent on this channel, or its last send is older than the window."""
    sent = parse_ts(last_at)
    return sent is None or (now - sent) > window


def within_window(last_at: str | None, now: datetime, window: timedelta) -> bool:
    """The inverse of :func:`fresh` for the "already reported" direction:
    False when there is no previous timestamp at all."""
    previous = parse_ts(last_at)
    return previous is not None and (now - previous) <= window


def plan(
    pending: Sequence[Event],
    *,
    last_sent: Mapping[str, str] | None = None,
    attempts: Mapping[int, int] | None = None,
    now: datetime | None = None,
    window: timedelta = THROTTLE_WINDOW,
    max_attempts: int = MAX_ATTEMPTS,
) -> Plan:
    """Turn one channel's pending events into the messages it should get.

    ``last_sent`` is ``dedup_key -> ts`` of previous successful deliveries,
    ``attempts`` is ``event_id -> failed rounds``. Both default to empty, so a
    first-ever dispatch is also the simplest call.
    """
    stamp = now or datetime.now(timezone.utc)
    tries = dict(attempts or {})

    groups: dict[str, list[Event]] = {}
    for event in pending:
        groups.setdefault(event.dedup_key, []).append(event)

    deliveries: list[Delivery] = []
    for key, group in groups.items():
        if fresh((last_sent or {}).get(key), stamp, window):
            head, *rest = group
            deliveries.append(Delivery(head, folded=(head.id,), reason="first"))
        else:
            rest = group
        if rest:
            notice = merge_notice(rest, stamp)
            deliveries.append(
                Delivery(notice, folded=tuple(e.id for e in rest), reason="repeat")
            )

    stuck = tuple(
        e
        for e in pending
        if not is_self_alert(e)
        and e.severity in ESCALATE_SEVERITIES
        and tries.get(e.id, 0) >= max_attempts
    )

    return Plan(deliveries=tuple(deliveries), stuck=stuck)
