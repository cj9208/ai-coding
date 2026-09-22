"""Channel adapters — one registry entry each (design §3, D-3).

Every channel is the same shape: "take a rendered event, fire one delivery,
raise :class:`ChannelError` on failure". The telegram and ping adapters of
M1/M2 add a class and a registry line beside ``stdout`` — dispatch, ledger
and task code never change.
"""

from __future__ import annotations

from typing import Mapping

from .base import Channel, ChannelError, render
from .stdout import StdoutChannel

_REGISTRY: Mapping[str, type[Channel]] = {"stdout": StdoutChannel}


def build(name: str) -> Channel:
    try:
        return _REGISTRY[name]()
    except KeyError:
        available = ", ".join(sorted(_REGISTRY))
        raise ChannelError(f"unknown channel '{name}' (available: {available})")


def names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["Channel", "ChannelError", "build", "names", "render"]
