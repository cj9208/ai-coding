"""The emit facade: persists, derives dedup_key, and never raises (§4.2)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from notify import emit
from notify.ledger import default


def test_emit_appends_and_returns_id(ledger_dir: Path) -> None:
    event_id = emit("quantdesk", "record.batch", rows=240, stream="liquidations")

    assert event_id is not None and event_id > 0
    (event,) = default().recent(1)
    assert event.project == "quantdesk"
    assert event.kind == "record.batch"
    assert event.payload == {"rows": 240, "stream": "liquidations"}


def test_dedup_key_defaults_to_project_kind(ledger_dir: Path) -> None:
    emit("quantdesk", "ws.silent")
    assert default().recent(1)[0].dedup_key == "quantdesk:ws.silent"


def test_dedup_key_can_narrow_per_stream(ledger_dir: Path) -> None:
    emit("quantdesk", "ws.silent", dedup_key="quantdesk:ws.silent:liquidations")
    assert default().recent(1)[0].dedup_key == "quantdesk:ws.silent:liquidations"


def test_emit_never_raises_on_corrupt_ledger(
    ledger_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The design's hard contract: a broken notification layer must not
    # become the task's failure — drop with one honest stderr line.
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "events.db").write_bytes(b"this is not a database" * 40)

    assert emit("quantdesk", "record.batch", rows=1) is None

    err = capsys.readouterr().err
    assert "emit failed" in err and "dropped" in err


def test_emit_signature_leaks_no_channel_concepts() -> None:
    # D-1's boundary: task-side callers must never see a channel word.
    params = set(inspect.signature(emit).parameters) | {"payload"}
    leaked = {"telegram", "token", "chat_id", "proxy", "channel", "url"} & {
        p.lower() for p in params
    }
    assert not leaked
