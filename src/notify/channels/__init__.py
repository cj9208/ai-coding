"""Channel adapters — one registry entry each (design §3, D-3).

Every channel is the same shape: "take a rendered event, fire one delivery,
raise :class:`ChannelError` on failure". ``telegram`` is the proof that the
shape holds — it arrived without a single change to dispatch, the ledger or
any emitter. The ping adapter of M2 is one more line here.
"""

from __future__ import annotations

from typing import Mapping

from .base import Channel, ChannelError, render
from .stdout import StdoutChannel
from .telegram import TelegramChannel

_REGISTRY: Mapping[str, type[Channel]] = {
    "stdout": StdoutChannel,
    "telegram": TelegramChannel,
}


def build(name: str) -> Channel:
    try:
        return _REGISTRY[name]()
    except KeyError:
        available = ", ".join(sorted(_REGISTRY))
        raise ChannelError(f"unknown channel '{name}' (available: {available})")


def names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["Channel", "ChannelError", "build", "names", "render"]
