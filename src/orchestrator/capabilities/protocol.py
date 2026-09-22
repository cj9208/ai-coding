"""The two structural types the runtime depends on (typing.Protocol, not
ABC — structural typing keeps adapters dumb, per the design's module map).

Both methods are **coroutines** (05a step 5): the slow points they wrap
(an ``llm_client`` call, a rag retrieve+generate) are awaited, which is
what lets the harness be embedded in an ASGI host without ever calling
``asyncio.run`` inside a running loop. Implementations that do sync
work (SQLite reads) wrap it in ``asyncio.to_thread``.

Rules enforced by ``runtime.py``/``execution`` path, not by these protocols:
capabilities never call each other, never see or mutate the envelope, and
never decide their own retry — they report structured status + code and the
tables decide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..contracts import CapabilityContext, CapabilityResult, FrontHalfOutput

if TYPE_CHECKING:
    from ..contracts import RequestEnvelope


@runtime_checkable
class Capability(Protocol):
    async def run(self, ctx: CapabilityContext) -> CapabilityResult: ...


class FrontHalf(Protocol):
    """The interpretation seam: safety + normalize + (M1) flash model.

    M0 ships a scripted fake (``fake.FakeFrontHalf``); M1 replaces it with
    ``interpret.py`` over ``llm_client`` — the runtime never changes.
    ``escalated=True`` means this pass is the stronger-model re-interpret
    requested by routing (CH01 row 5/6).
    """

    async def interpret(
        self,
        envelope: "RequestEnvelope",
        *,
        answer: str | None = None,
        escalated: bool = False,
    ) -> FrontHalfOutput: ...
