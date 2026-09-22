"""Zero-LLM plug-ins that make the whole control loop testable in M0 —
the harness half of the architecture must be provable before any model is
wired in (design "Milestones": M0 is zero-LLM).

``FakeFrontHalf`` is a script: each ``interpret`` call consumes one turn
dict; when the script is exhausted the last turn repeats (so a resume
after a transient failure doesn't need a second entry). Field names match
``FrontHalfOutput`` so golden-case files read naturally.
"""

from __future__ import annotations

import time
from typing import Any

from ..contracts import (
    CapabilityContext,
    CapabilityResult,
    DeterministicSignals,
    FrontHalfOutput,
    InterpretationRecord,
    ModelSignals,
    RequestEnvelope,
)
from ..ids import new_id


class FakeFrontHalf:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        if not turns:
            raise ValueError("FakeFrontHalf needs at least one scripted turn")
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def interpret(
        self,
        envelope: RequestEnvelope,
        *,
        answer: str | None = None,
        escalated: bool = False,
    ) -> FrontHalfOutput:
        idx = min(len(self.calls), len(self._turns) - 1)
        turn = self._turns[idx]
        self.calls.append({"answer": answer, "escalated": escalated})
        now_ms = int(time.time() * 1000)
        det_kwargs = {
            k: turn[k]
            for k in (
                "alias_hits",
                "rule_hits",
                "top_match_score",
                "top2_gap",
                "candidate_count",
            )
            if k in turn
        }
        model = ModelSignals(**turn.get("model", {}))
        interpretation = InterpretationRecord(
            interpretation_id=new_id("int"),
            request_id=envelope.request_id,
            timestamp_ms=now_ms,
            normalized_query=turn.get("normalized_query")
            or answer
            or envelope.original_input.text,
            task_type=turn.get("task_type", "test"),
            deterministic=DeterministicSignals(**det_kwargs),
            model=model,
            target_entity_guess=turn.get("target_entity_guess"),
            requested_attributes=list(turn.get("requested_attributes", [])),
            interpretation_summary=turn.get("interpretation_summary", ""),
        )
        out_kwargs = {
            k: turn[k]
            for k in (
                "safety",
                "action_type",
                "risk",
                "missing_required_constraint",
                "user_resolvable_ambiguity",
                "clarification_question",
                "constraints",
            )
            if k in turn
        }
        return FrontHalfOutput(interpretation=interpretation, **out_kwargs)


class EchoCapability:
    """Trivially grounded read capability: echoes the normalized query."""

    def __init__(self) -> None:
        self.contexts: list[CapabilityContext] = []

    async def run(self, ctx: CapabilityContext) -> CapabilityResult:
        self.contexts.append(ctx)
        return CapabilityResult(
            status="success",
            output={"text": ctx.normalized_query},
            tool_steps=["echo.run"],
        )


class ScriptedCapability:
    """Pops one scripted CapabilityResult dict per call; the last result
    repeats when the script runs out. Used to drive the retry / switch /
    validation rows without touching policy.py."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        if not results:
            raise ValueError("ScriptedCapability needs at least one result")
        self._results = list(results)
        self.contexts: list[CapabilityContext] = []

    async def run(self, ctx: CapabilityContext) -> CapabilityResult:
        self.contexts.append(ctx)
        idx = min(len(self.contexts) - 1, len(self._results) - 1)
        return CapabilityResult(**self._results[idx])
