"""Telegram: the first real channel (D-3) — one HTTP POST per message.

Same shape as :mod:`stdout`, which is the point of the adapter interface: the
ledger, dispatch and every emitter are unchanged by a channel existing.

Credentials come from the environment here rather than in ``config.py``, so
the package's one path/switch module never learns what a channel needs
(§4.7). ``TELEGRAM_PROXY`` is optional and an empty value means "no proxy":
the design assumed a local HTTP proxy was required, the 2026-09-23 real-machine
smoke found this host reaches ``api.telegram.org`` directly while port 7890
was not even listening — a hard-coded proxy would have been a dead channel.

Rate limiting (§4.4) is this module's business, not policy's: Telegram allows
roughly 1 msg/s per target and answers 429 with ``retry_after``, so a bounded
number of waits happens here and anything still failing becomes a
:class:`ChannelError` the ledger records — dispatch retries next round.
"""

from __future__ import annotations

import os
import time

import httpx

from ..events import Event
from .base import ChannelError, render

API_ROOT = "https://api.telegram.org"

#: Telegram rejects a longer message body
MAX_TEXT = 4096

#: 429s waited out inside one send() before giving up
MAX_RATE_LIMIT_WAITS = 2

DEFAULT_TIMEOUT = 20.0


def _retry_after(response: httpx.Response) -> float:
    try:
        body = response.json()
    except ValueError:
        return 1.0
    parameters = body.get("parameters") or {}
    try:
        return float(parameters.get("retry_after", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _text(event: Event) -> str:
    body = render(event)
    return body if len(body) <= MAX_TEXT else body[: MAX_TEXT - 1] + "…"


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self.token = (
            os.getenv("TELEGRAM_BOT_TOKEN", "").strip() if token is None else token
        )
        self.chat_id = (
            os.getenv("TELEGRAM_CHAT_ID", "").strip() if chat_id is None else chat_id
        )
        raw_proxy = os.getenv("TELEGRAM_PROXY", "").strip() if proxy is None else proxy
        self.proxy = raw_proxy or None
        self.timeout = timeout
        #: tests inject httpx.MockTransport; production leaves it None
        self.transport = transport

    def _client(self) -> httpx.Client:
        # One client per message: a dispatch round sends a handful, and this
        # keeps the adapter free of lifecycle the Channel protocol doesn't have.
        return httpx.Client(
            proxy=self.proxy, timeout=self.timeout, transport=self.transport
        )

    def send(self, event: Event) -> None:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.token),
                ("TELEGRAM_CHAT_ID", self.chat_id),
            )
            if not value
        ]
        if missing:
            raise ChannelError(
                f"telegram not configured: missing {', '.join(missing)} in .env"
            )
        self._post("sendMessage", {"chat_id": self.chat_id, "text": _text(event)})

    def _post(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        url = f"{API_ROOT}/bot{self.token}/{method}"
        waits = 0
        with self._client() as client:
            while True:
                try:
                    response = client.post(url, data=payload)
                except httpx.HTTPError as exc:
                    raise ChannelError(f"telegram {method} unreachable: {exc}") from exc
                if response.status_code == 429 and waits < MAX_RATE_LIMIT_WAITS:
                    waits += 1
                    time.sleep(_retry_after(response))
                    continue
                break

        if response.status_code != 200:
            raise ChannelError(
                f"telegram {method} -> HTTP {response.status_code}:"
                f" {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelError(f"telegram {method} returned non-JSON") from exc
        if not body.get("ok"):
            raise ChannelError(f"telegram {method} refused: {body.get('description')}")
        return body.get("result") or {}
