"""The escalation family (CH01): building the handoff packet and rendering
it as markdown. DP-3 — that is *all* handoff is in v1: a persisted object
plus this export, no ticket system.

The packet's six sections mirror the CH01 packet spec one-to-one, so a
human picking up the markdown sees exactly what the runtime knew.
"""

from __future__ import annotations

import time
from typing import Any

from ..contracts import (
    AttemptHistory,
    BudgetState,
    ConversationContext,
    CurrentInterpretation,
    HandoffPacket,
    HandoffReason,
    RecommendedNextStep,
    RequestEnvelope,
)
from ..ids import new_id


def build_handoff_packet(
    envelope: RequestEnvelope,
    *,
    reason_code: str,
    reason_summary: str,
    objects: list[dict[str, Any]],
    recommended_next_step: str = "",
    now_ms: int | None = None,
) -> HandoffPacket:
    """Project the request's stored objects onto the six packet sections."""
    routing_ids: list[str] = []
    execution_ids: list[str] = []
    clarification_history: list[str] = []
    latest_query = envelope.original_input.text
    candidate_entities: list[str] = []
    for item in objects:
        kind, payload = item["kind"], item["payload"]
        if kind == "routing":
            routing_ids.append(str(payload.get("routing_decision_id", "")))
        elif kind == "execution":
            execution_ids.append(str(payload.get("execution_id", "")))
        elif kind == "clarification_answer":
            clarification_history.append(str(payload.get("answer", "")))
        elif kind == "interpretation":
            latest_query = str(payload.get("normalized_query", latest_query))
            flags = payload.get("model", {}).get("ambiguity_flags", [])
            alts = payload.get("model", {}).get("alternative_interpretations", [])
            candidate_entities = [str(f) for f in flags] + [str(a) for a in alts]
    c = envelope.attempt_counters
    return HandoffPacket(
        handoff_id=new_id("handoff"),
        request_id=envelope.request_id,
        timestamp_ms=now_ms if now_ms is not None else int(time.time() * 1000),
        reason=HandoffReason(code=reason_code, summary=reason_summary),
        conversation_context=ConversationContext(
            original_input=envelope.original_input.text,
            clarification_history=clarification_history,
        ),
        current_interpretation=CurrentInterpretation(
            normalized_query=latest_query,
            candidate_entities=candidate_entities,
        ),
        attempt_history=AttemptHistory(
            routing_decision_ids=routing_ids,
            execution_ids=execution_ids,
        ),
        budget_state=BudgetState(
            total_loops_used=c.total_loops,
            clarification_turns_used=c.clarification_turns,
            model_escalations_used=c.model_escalations,
            execution_retries_used=c.execution_retries,
        ),
        recommended_next_step=RecommendedNextStep(
            type="human_review", payload=recommended_next_step
        ),
    )


def render_handoff_markdown(packet: HandoffPacket) -> str:
    """The six CH01 packet sections, scan-friendly (DP-3's only export)."""
    p = packet
    lines = [
        f"# Handoff {p.handoff_id}",
        "",
        f"- request: `{p.request_id}`",
        f"- created: {p.timestamp_ms}",
        "",
        "## 1. Reason for handoff",
        f"- code: `{p.reason.code}`",
        f"- summary: {p.reason.summary}",
        "",
        "## 2. Conversation context",
        f"- original input: {p.conversation_context.original_input}",
        "- clarification history:",
    ]
    hist = p.conversation_context.clarification_history
    lines.extend([f"  {i + 1}. {a}" for i, a in enumerate(hist)] or ["  (none)"])
    lines += [
        "",
        "## 3. Current interpretation",
        f"- normalized query: {p.current_interpretation.normalized_query}",
        "- candidate entities / ambiguities:",
    ]
    ents = p.current_interpretation.candidate_entities
    lines.extend([f"  - {e}" for e in ents] or ["  (none)"])
    routes = ", ".join(f"`{r}`" for r in p.attempt_history.routing_decision_ids)
    execs = ", ".join(f"`{e}`" for e in p.attempt_history.execution_ids)
    lines += [
        "",
        "## 4. Attempt history",
        f"- routing decisions: {routes or '(none)'}",
        f"- executions: {execs or '(none)'}",
        "",
        "## 5. Budget state",
        f"- total loops used: {p.budget_state.total_loops_used}",
        f"- clarification turns used: {p.budget_state.clarification_turns_used}",
        f"- model escalations used: {p.budget_state.model_escalations_used}",
        f"- execution retries used: {p.budget_state.execution_retries_used}",
        "",
        "## 6. Recommended next step",
        f"- {p.recommended_next_step.type}: {p.recommended_next_step.payload or '(none)'}",
        "",
    ]
    return "\n".join(lines)
