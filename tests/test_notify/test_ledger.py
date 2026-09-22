"""Ledger read/write: alert-first pending, delivery idempotence, 10k scale."""

from __future__ import annotations

from pathlib import Path

from notify.events import Event
from notify.ledger import open_ledger


def _events(*kinds: tuple[str, str, str]) -> list[Event]:
    return [Event.new(project, kind, severity=sev) for project, kind, sev in kinds]


def test_pending_is_alert_first(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(
        _events(
            ("quantdesk", "record.batch", "info"),
            ("quantdesk", "ws.silent", "alert"),
            ("quantdesk", "record.gap", "warn"),
        )
    )

    pending = led.pending_for("stdout")
    assert [e.severity.value for e in pending] == ["alert", "warn", "info"]


def test_sent_events_are_not_re_delivered(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(_events(("quantdesk", "record.batch", "info")))
    (event,) = led.pending_for("stdout")

    led.mark_delivery(event.id, "stdout", "sent")
    assert led.pending_for("stdout") == []

    # a second channel still owes its own delivery (§4.3: per-event-per-channel)
    assert [e.id for e in led.pending_for("telegram")] == [event.id]


def test_failed_delivery_can_be_retried_and_row_updated(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(_events(("quantdesk", "ws.silent", "alert")))
    (event,) = led.pending_for("stdout")

    led.mark_delivery(event.id, "stdout", "failed", "proxy down")
    assert [e.id for e in led.pending_for("stdout")] == [event.id]

    led.mark_delivery(event.id, "stdout", "sent")
    assert led.pending_for("stdout") == []
    # the retry updated the one row rather than appending an audit trail
    assert led.deliveries_for(event.id) == [("stdout", "sent", "")]


def test_ten_thousand_events_summary_and_status(ledger_dir: Path) -> None:
    # M0 acceptance: after emitting 10k, status counts must be right.
    led = open_ledger(ledger_dir / "events.db")
    batch = [
        Event.new("quantdesk", "record.batch", payload={"i": i}) for i in range(10_000)
    ]
    led.insert_many(batch)
    assert led.total() == 10_000

    rows = led.summary("quantdesk")
    assert rows == [("quantdesk", "record.batch", "info", 10_000, batch[-1].ts)]


def test_summary_groups_by_project(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(
        _events(
            ("quantdesk", "record.batch", "info"),
            ("quantdesk", "record.batch", "info"),
            ("orchestrator", "run.turn", "info"),
        )
    )
    grouped = {(p, k, s): n for p, k, s, n, _ in led.summary()}
    assert grouped == {
        ("quantdesk", "record.batch", "info"): 2,
        ("orchestrator", "run.turn", "info"): 1,
    }
