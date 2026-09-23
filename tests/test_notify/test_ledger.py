"""Ledger read/write: alert-first pending, delivery idempotence, 10k scale."""

from __future__ import annotations

import sqlite3
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

    pending = led.pending_for("stdout", severities=("alert", "warn", "info"))
    assert [e.severity.value for e in pending] == ["alert", "warn", "info"]


def test_info_waits_for_the_digest_by_default(ledger_dir: Path) -> None:
    # §4.4 is a query-side filter in M1: what never enters pending_for can never
    # be mis-planned, so policy only ever sees alert and warn.
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(
        _events(
            ("quantdesk", "record.batch", "info"),
            ("quantdesk", "ws.silent", "alert"),
        )
    )
    assert [e.severity.value for e in led.pending_for("stdout")] == ["alert"]


def test_an_urgent_info_event_joins_the_immediate_path(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert(
        Event.new(
            "quantdesk", "record.batch", severity="info", payload={"urgent": True}
        )
    )
    led.insert(
        Event.new(
            "quantdesk", "record.batch", severity="info", payload={"urgent": False}
        )
    )
    led.insert(Event.new("quantdesk", "record.batch", severity="info", payload={}))

    pending = led.pending_for("stdout")
    assert [e.payload.get("urgent") for e in pending] == [True]


def test_sent_events_are_not_re_delivered(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(_events(("quantdesk", "ws.silent", "alert")))
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


def test_failed_attempts_accumulate_and_reset_on_success(ledger_dir: Path) -> None:
    # §4.4 escalates on "pending 超过 3 轮 dispatch": the counter is what makes
    # that question answerable, so only a failure advances it and a success
    # clears it.
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(_events(("quantdesk", "ws.silent", "alert")))
    (event,) = led.pending_for("stdout")

    for round_number in (1, 2, 3):
        led.mark_delivery(event.id, "stdout", "failed", "proxy down")
        assert led.attempts("stdout", [event.id]) == {event.id: round_number}

    led.mark_delivery(event.id, "stdout", "sent")
    assert led.attempts("stdout", [event.id]) == {event.id: 0}
    assert led.attempts("telegram", [event.id]) == {}


def test_last_sent_by_key_is_per_channel(ledger_dir: Path) -> None:
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(
        _events(
            ("quantdesk", "ws.silent", "alert"),
            ("quantdesk", "record.batch", "info"),
        )
    )
    alert, _info = led.pending_for("stdout", severities=("alert", "warn", "info"))
    led.mark_delivery(alert.id, "stdout", "sent")

    assert list(led.last_sent_by_key("stdout")) == ["quantdesk:ws.silent"]
    # stdout's history says nothing about telegram's throttle window
    assert led.last_sent_by_key("telegram") == {}


def test_last_event_ts_by_key_sees_rows_that_were_never_delivered(
    ledger_dir: Path,
) -> None:
    # rule idempotence keys off the ledger row, not the delivery (§4.5)
    led = open_ledger(ledger_dir / "events.db")
    led.insert_many(_events(("quantdesk", "record.batch.silent", "alert")))
    (event,) = led.pending_for("stdout")
    assert led.last_event_ts_by_key() == {event.dedup_key: event.ts}


def test_an_m0_ledger_gains_the_attempts_column(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    led = open_ledger(db)
    led.insert_many(_events(("quantdesk", "ws.silent", "alert")))
    (event,) = led.pending_for("stdout")
    led.mark_delivery(event.id, "stdout", "failed", "recorded before the upgrade")
    led.dispose()

    with sqlite3.connect(db) as conn:  # roll the file back to its M0 shape
        conn.execute("ALTER TABLE deliveries DROP COLUMN attempts")
        conn.commit()

    upgraded = open_ledger(db)
    # the counter is re-added at zero, so the worst the migration costs is one
    # extra dispatch round before an undeliverable alert escalates
    assert upgraded.attempts("stdout", [event.id]) == {event.id: 0}
    assert upgraded.deliveries_for(event.id) == [
        ("stdout", "failed", "recorded before the upgrade")
    ]
    upgraded.dispose()
