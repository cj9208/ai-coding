"""CLI round trip: emit -> status -> dispatch, idempotent re-dispatch.

The click commands are driven through ``CliRunner``, so exit codes come from
``result.exit_code`` and the printed lines from ``result.stdout`` /
``result.stderr``.

The M1 half of this file is where the three §4/§5 mechanisms meet for real —
rules writing an alert, policy folding a burst, escalation noticing a dead
channel — since ``policy.plan`` and ``rules.evaluate`` are each unit-tested as
pure functions and never see a ledger.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from notify.cli import cli
from notify.ledger import open_ledger


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
    # M1 (§4.4): the alert goes out now, the info event waits for the digest —
    # and waiting is a delivery decision, not a lost row (status still counts it)
    assert "ws.silent" in out
    assert "record.batch" not in out
    assert "stdout: messages 1, sent 1, failed 0" in out

    # idempotence: re-dispatch delivers nothing that is already sent (§5 M0)
    result = run("dispatch")
    assert result.exit_code == 0
    assert "stdout: messages 0, sent 0, failed 0" in result.stdout


def test_urgent_info_rides_the_immediate_path(ledger_dir: Path) -> None:
    # the escape hatch §4.4 names: urgent=true is what lets an info event leave
    # with the alerts — and payload JSON types survive emit -> dispatch
    assert (
        run("emit", "quantdesk", "record.batch", "rows=240", "urgent=true").exit_code
        == 0
    )
    result = run("dispatch")
    assert '"rows": 240' in result.stdout
    assert '"urgent": true' in result.stdout


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
    assert run("emit", "quantdesk", "ws.silent", "--severity", "alert").exit_code == 0
    result = run("dispatch", "--channels", "stdout")
    assert result.exit_code == 0
    assert "stdout: messages 1, sent 1, failed 0" in result.stdout


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


# -- M1: rules, throttling and escalation through the real entry point ----


SILENCE_RULE = """
expectations:
  - project: quantdesk
    kind: record.batch
    max_silence: 3h
    message: "no batches"
"""


def _expectations(tmp_path: Path, body: str) -> str:
    path = tmp_path / "expectations.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _counts(ledger_dir: Path, project: str) -> dict[tuple[str, str, str], int]:
    led = open_ledger(ledger_dir / "events.db")
    try:
        return {(p, k, s): n for p, k, s, n, _ in led.summary(project)}
    finally:
        led.dispose()


def _deliveries(ledger_dir: Path, event_id: int) -> list[tuple[str, str, str]]:
    led = open_ledger(ledger_dir / "events.db")
    try:
        return led.deliveries_for(event_id)
    finally:
        led.dispose()


def _telegram_cannot_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    # a channel that cannot even be configured fails exactly like a dead wire,
    # and it costs the suite no network round trip
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_dispatch_emits_and_delivers_a_silence_alert(
    ledger_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §4.5: an empty ledger *is* the gap, and the alert notify writes for itself
    # rides the same delivery path as any other event
    monkeypatch.setenv("NOTIFY_EXPECTATIONS", _expectations(tmp_path, SILENCE_RULE))
    result = run("dispatch")
    assert result.exit_code == 0, result.stdout
    assert "rules: emitted 1 silence alert(s)" in result.stdout
    assert "quantdesk.record.batch.silent" in result.stdout
    assert "stdout: messages 1, sent 1" in result.stdout
    assert _counts(ledger_dir, "quantdesk") == {
        ("quantdesk", "record.batch.silent", "alert"): 1
    }


def test_a_running_producer_keeps_the_rules_quiet(
    ledger_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFY_EXPECTATIONS", _expectations(tmp_path, SILENCE_RULE))
    assert run("emit", "quantdesk", "record.batch", "rows=240").exit_code == 0
    assert "rules: emitted" not in run("dispatch").stdout


def test_the_same_silence_alerts_once_per_window(
    ledger_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFY_EXPECTATIONS", _expectations(tmp_path, SILENCE_RULE))
    assert "rules: emitted" in run("dispatch").stdout
    again = run("dispatch")
    assert "rules: emitted" not in again.stdout
    assert "stdout: messages 0, sent 0" in again.stdout
    assert _counts(ledger_dir, "quantdesk") == {
        ("quantdesk", "record.batch.silent", "alert"): 1
    }


def test_a_burst_of_five_becomes_two_messages(ledger_dir: Path) -> None:
    # §5 M1 acceptance: 5 same-key alerts -> one immediate, one merged notice
    for _ in range(5):
        assert (
            run("emit", "quantdesk", "ws.silent", "--severity", "alert").exit_code == 0
        )
    assert "stdout: messages 2, sent 5" in run("dispatch").stdout

    # the fold is visible in the audit trail, so "did #5 reach me" stays
    # answerable even though no message was sent for it on its own
    assert _deliveries(ledger_dir, 5) == [
        ("stdout", "sent", "merged 4 events into one notice")
    ]
    assert run("dispatch").stdout.startswith("stdout: messages 0, sent 0")


def test_an_alert_that_fails_everywhere_becomes_a_self_alert(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §4.4 escalation, trigger 1: one configured channel, delivery refused
    _telegram_cannot_deliver(monkeypatch)
    assert run("emit", "quantdesk", "ws.silent", "--severity", "alert").exit_code == 0

    result = run("dispatch", "--channels", "telegram")
    assert "telegram: messages 1, sent 0, failed 1" in result.stdout
    assert "escalated: notify:delivery.failed" in result.stdout
    assert _counts(ledger_dir, "notify") == {("notify", "delivery.failed", "alert"): 1}

    # the self-alert must not amplify: it fails on the same dead channel as the
    # alert it reports, and the throttle window holds the repeat down
    again = run("dispatch", "--channels", "telegram")
    assert "escalated" not in again.stdout
    assert "escalation throttled" in again.stdout
    assert _counts(ledger_dir, "notify") == {("notify", "delivery.failed", "alert"): 1}


def test_three_failing_rounds_on_one_channel_still_escalate(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §4.4 escalation, trigger 2: stdout succeeds, so "failed everywhere" never
    # holds — only the per-channel attempts counter can notice telegram is dead
    _telegram_cannot_deliver(monkeypatch)
    assert run("emit", "quantdesk", "ws.silent", "--severity", "alert").exit_code == 0

    first = run("dispatch", "--channels", "stdout,telegram").stdout
    assert "stdout: messages 1, sent 1" in first
    assert "telegram: messages 1, sent 0, failed 1" in first
    assert "escalated" not in first

    for _round in range(2):  # rounds 2 and 3: only telegram still owes the alert
        out = run("dispatch", "--channels", "stdout,telegram").stdout
        assert "stdout: messages 0, sent 0" in out
        assert "telegram: messages 1, sent 0, failed 1" in out
        assert "escalated" not in out

    fourth = run("dispatch", "--channels", "stdout,telegram").stdout
    assert "escalated: notify:delivery.failed" in fourth


def test_check_reports_config_and_rules_without_touching_a_channel(
    ledger_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _telegram_cannot_deliver(monkeypatch)
    monkeypatch.setenv("NOTIFY_CHANNELS", "stdout,telegram")
    monkeypatch.setenv("NOTIFY_EXPECTATIONS", _expectations(tmp_path, SILENCE_RULE))
    result = run("check")
    assert result.exit_code == 0, result.stdout
    assert "channels  stdout, telegram" in result.stdout
    assert "[on ] quantdesk.record.batch > 3h" in result.stdout
    assert "firing now: 1" in result.stdout
    assert "probe" not in result.stdout
    # a dry run changes nothing: the rule fired on screen, not on the ledger
    assert _counts(ledger_dir, "quantdesk") == {}


def test_check_fails_on_an_unknown_channel(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFY_CHANNELS", "nope")
    result = run("check")
    assert result.exit_code == 1
    assert "unknown channel 'nope'" in result.stderr


def test_check_send_probes_the_wire_and_reports_instead_of_raising(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _telegram_cannot_deliver(monkeypatch)
    monkeypatch.setenv("NOTIFY_CHANNELS", "stdout,telegram")
    result = run("check", "--send")
    assert result.exit_code == 0, result.stdout
    assert "probe stdout: ok" in result.stdout
    assert "probe telegram: FAILED" in result.stdout
    # a failing probe names the env key to fix, never a value
    assert "TELEGRAM_BOT_TOKEN" in result.stdout
