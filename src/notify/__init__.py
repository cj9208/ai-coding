"""notify — shared event-ledger and delivery layer.

Implementation contract is ``docs/notify-design.md`` (D-1..D-6 are owner
decisions, do not re-derive). The task side sees exactly one facade::

    from notify import emit
    emit("quantdesk", "record.batch", rows=240, stream="liquidations")

which appends a structured event to the local SQLite ledger and *never*
raises — a notification-layer failure must not become a task failure
(§4.2). Delivery to channels happens only in ``notify dispatch``, a
short-lived process a scheduler launches (§1's no-daemon rule): the ledger
is the source of truth (D-5), channels are pluggable adapters, and task
code never touches a channel concept.
"""

from .events import Event, Severity, emit

__all__ = ["Event", "Severity", "emit"]
