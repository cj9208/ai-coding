"""CLI round trip: emit -> status -> dispatch, idempotent re-dispatch.

The click commands are driven through ``CliRunner``, so exit codes come from
``result.exit_code`` and the printed lines from ``result.stdout`` /
``result.stderr``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from notify.cli import cli


def run(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args))


def test_emit_status_dispatch_roundtrip(ledger_dir: Path) -> None:
    result = run("emit", "quantdesk", "record.batch", "rows=240", "stream=liquidations")
    assert result.exit_code == 0, result.output
    assert int(result.output.strip()) >= 1
    assert run("emit", "quantdesk", "ws.silent", "--severity", "alert").exit_code == 0

    result = run("status")
    assert result.exit_code == 0
    out = result.stdout
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

    result = run("dispatch")
    assert result.exit_code == 0
    out = result.stdout
    assert '"rows": 240' in out  # payload keeps its JSON types end to end
    assert "stdout: sent 2" in out
    # alert-first (§4.4): the silent-stream alert renders before the batch info
    assert out.index("ws.silent") < out.index("record.batch")

    # idempotence: re-dispatch delivers nothing that is already sent (§5 M0)
    result = run("dispatch")
    assert result.exit_code == 0
    assert "stdout: sent 0" in result.stdout


def test_status_empty_ledger(ledger_dir: Path) -> None:
    result = run("status")
    assert result.exit_code == 0
    assert "ledger empty" in result.stdout


def test_dispatch_channel_list_from_env(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFY_CHANNELS", "nope")
    assert run("emit", "quantdesk", "record.batch").exit_code == 0
    result = run("dispatch")
    assert result.exit_code == 1
    assert "unknown channel 'nope'" in result.stderr


def test_dispatch_channels_flag_overrides_env(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFY_CHANNELS", "nope")
    assert run("emit", "quantdesk", "record.batch").exit_code == 0
    result = run("dispatch", "--channels", "stdout")
    assert result.exit_code == 0
    assert "stdout: sent 1" in result.stdout


def test_emit_rejects_malformed_payload(ledger_dir: Path) -> None:
    result = run("emit", "quantdesk", "record.batch", "no-equals-sign")
    assert result.exit_code == 1
    assert "key=value" in result.stderr


def test_emit_rejects_reserved_payload_keys(ledger_dir: Path) -> None:
    # live smoke found the collision: dedup_key=... as payload hit **kwargs
    result = run("emit", "quantdesk", "ws.silent", "dedup_key=x")
    assert result.exit_code == 1
    err = result.stderr
    assert "reserved" in err and "--dedup-key" in err
