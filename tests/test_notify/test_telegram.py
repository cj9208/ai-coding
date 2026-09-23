"""The telegram adapter: one POST, and every way that can fail stays a
ChannelError the ledger can record (§4.4 rate limits, §6.1 proxy).

No test here touches the network: ``httpx.MockTransport`` answers the client,
and the real .env is always overridden — a green suite must not depend on
credentials, and a red one must not leak them.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from notify import channels
from notify.channels.base import ChannelError
from notify.channels.telegram import MAX_TEXT, TelegramChannel
from notify.events import Event, Severity

#: both constants are fakes: the fixture below overrides the environment, so a
#: green suite never depends on — and never records — anyone's real credentials
TOKEN = "123456:TEST-token-not-a-secret"
CHAT_ID = "123456789"


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)


def _event(**overrides: object) -> Event:
    base: dict[str, object] = {
        "id": 1,
        "ts": "2026-09-23T10:00:00+00:00",
        "project": "quantdesk",
        "kind": "ws.silent",
        "severity": Severity.ALERT,
        "dedup_key": "quantdesk:ws.silent",
        "payload": {"stream": "liquidations"},
    }
    base.update(overrides)
    return Event(**base)  # type: ignore[arg-type]


def _channel(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> TelegramChannel:
    transport = httpx.MockTransport(handler)
    return TelegramChannel(transport=transport, **kwargs)  # type: ignore[arg-type]


def _sent_form(request: httpx.Request) -> dict[str, str]:
    form = parse_qs(request.content.decode("utf-8"))
    return {key: values[0] for key, values in form.items()}


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})


def test_send_posts_the_rendered_event_to_the_configured_chat() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    _channel(handler).send(_event())

    (request,) = seen
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = _sent_form(request)
    assert body["chat_id"] == CHAT_ID
    assert '[alert] quantdesk.ws.silent {"stream": "liquidations"}' in body["text"]


def test_missing_credentials_fail_as_a_channel_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    channel = _channel(_ok)
    with pytest.raises(ChannelError) as excinfo:
        channel.send(_event())
    # names the env keys to fix, and never prints a value
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)
    assert "TELEGRAM_CHAT_ID" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (httpx.Response(502, text="bad gateway"), "HTTP 502"),
        (
            httpx.Response(200, json={"ok": False, "description": "chat not found"}),
            "chat not found",
        ),
        (httpx.Response(200, text="not json at all"), "non-JSON"),
    ],
)
def test_a_failed_delivery_raises_rather_than_swallowing(
    response: httpx.Response, needle: str
) -> None:
    channel = _channel(lambda _request: response)
    with pytest.raises(ChannelError, match=needle):
        channel.send(_event())


def test_a_dead_wire_becomes_a_channel_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("all proxy ports are closed")

    with pytest.raises(ChannelError, match="unreachable"):
        _channel(handler).send(_event())


def test_rate_limit_is_waited_out_inside_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §4.4: 429 backoff is adapter business — policy must not learn channel ids
    slept: list[float] = []
    monkeypatch.setattr("notify.channels.telegram.time.sleep", slept.append)
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"ok": False, "parameters": {"retry_after": 3}}
            )
        return _ok(_request)

    _channel(handler).send(_event())
    assert slept == [3.0]
    assert len(calls) == 2


def test_rate_limit_gives_up_after_a_bounded_number_of_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("notify.channels.telegram.time.sleep", lambda _s: None)
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})

    with pytest.raises(ChannelError, match="HTTP 429"):
        _channel(handler).send(_event())
    assert len(calls) == 3  # the first try plus MAX_RATE_LIMIT_WAITS waits


def test_long_payloads_are_cut_to_telegrams_own_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(request)

    big = {"blob": "x" * (MAX_TEXT * 2)}
    _channel(handler).send(_event(payload=big))
    text = _sent_form(seen[0])["text"]
    assert len(text) <= MAX_TEXT
    assert text.endswith("…")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),  # the 2026-09-23 finding: this host reaches Telegram directly
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
    ],
)
def test_proxy_is_optional(raw: str, expected: str | None) -> None:
    assert TelegramChannel(proxy=raw).proxy == expected


def test_an_absent_proxy_env_var_means_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)
    assert TelegramChannel().proxy is None


def test_the_registry_builds_it_like_any_other_channel() -> None:
    channel = channels.build("telegram")
    assert channel.name == "telegram"
    assert "telegram" in channels.names()
