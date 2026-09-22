"""The zero-dependency channel: print one line and be done.

M0's dispatch target, the permanent fallback, and the surface every
ledger/dispatch test runs on — a real channel is only ever *another class
with this shape*.
"""

from __future__ import annotations

import sys

from ..events import Event
from .base import render


class StdoutChannel:
    name = "stdout"

    def send(self, event: Event) -> None:
        print(render(event), file=sys.stdout, flush=True)
