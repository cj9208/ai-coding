"""CLI round trip: emit -> status -> dispatch, idempotent re-dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from notify.cli import main


def _drain(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def test_emit_status_dispatch_roundtrip(
    ledger_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["emit", "quantdesk", "record.batch", "rows=240", "stream=liquidations"])
        == 0
    )
    first_id = int(_drain(capsys))
    assert first_id >= 1
    assert main(["emit", "quantdesk", "ws.silent", "--severity", "alert"]) == 0
    _drain(capsys)

    assert main(["status"]) == 0
    out = _drain(capsys)
    rows = {
        (p[0], p[1], p[2]): p[3]
        for p in (
            line.split() for line in out.splitlines() if line.startswith("quantdesk")
        )
    }
    assert rows == {
        ("quantdesk", "record.batch", "info"): "1",
        ("quantdesk", "ws.silent", "alert"): "1",
    }
    assert "total 2 events" in out

    assert main(["dispatch"]) == 0
    out = _drain(capsys)
    assert '"rows": 240' in out  # payload keeps its JSON types end to end
    assert "stdout: sent 2" in out
    # alert-first (§4.4): the silent-stream alert renders before the batch info
    assert out.index("ws.silent") < out.index("record.batch")

    # idempotence: re-dispatch delivers nothing that is already sent (§5 M0)
    assert main(["dispatch"]) == 0
    assert "stdout: sent 0" in _drain(capsys)


def test_status_empty_ledger(
    ledger_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status"]) == 0
    assert "ledger empty" in _drain(capsys)


def test_dispatch_channel_list_from_env(
    ledger_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOTIFY_CHANNELS", "nope")
    main(["emit", "quantdesk", "record.batch"])
    _drain(capsys)
    assert main(["dispatch"]) == 1
    assert "unknown channel 'nope'" in capsys.readouterr().err


def test_dispatch_channels_flag_overrides_env(
    ledger_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOTIFY_CHANNELS", "nope")
    main(["emit", "quantdesk", "record.batch"])
    _drain(capsys)
    assert main(["dispatch", "--channels", "stdout"]) == 0
    assert "stdout: sent 1" in _drain(capsys)


def test_emit_rejects_malformed_payload(
    ledger_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["emit", "quantdesk", "record.batch", "no-equals-sign"]) == 1
    assert "key=value" in capsys.readouterr().err


def test_emit_rejects_reserved_payload_keys(
    ledger_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # live smoke found the collision: dedup_key=... as payload hit **kwargs
    assert main(["emit", "quantdesk", "ws.silent", "dedup_key=x"]) == 1
    err = capsys.readouterr().err
    assert "reserved" in err and "--dedup-key" in err
