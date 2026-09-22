"""Deterministic assembly of the routing table's inputs — the four signal
groups of CH02_03 collapsed into one :class:`ConfidenceAssessment` plus the
:class:`RoutingSignals` the table reads.

DP-9 lives here: ``confidence_state`` is chosen by *pattern* over the signal
groups (safety verdict first, then ambiguity evidence, then positive
evidence), never by a weighted formula. ``aggregate_score`` is computed and
recorded for later calibration against golden cases, but no table row is
allowed to condition on it.

Zero LLM: every input is already a plain fact on the envelope or on the
FrontHalfOutput; this module only orders them by precedence.
"""

from __future__ import annotations

from .contracts import (
    CapReached,
    ConfidenceAssessment,
    ConfidenceState,
    DeterministicSignals,
    ExecutionBudget,
    FrontHalfOutput,
    ModelSignals,
    RequestEnvelope,
    RoutingSignals,
    SafetyDecision,
)

# calibration constants (CH02_03 patterns; golden cases tighten them)
CLOSE_TOP2_GAP = 0.15
CLEAR_MODEL_CONFIDENCE = 0.6
STRONG_TOP_MATCH = 0.8


def confidence_of(
    safety: SafetyDecision,
    det: DeterministicSignals,
    model: ModelSignals,
) -> ConfidenceAssessment:
    """Map the signal groups onto one of the five confidence states."""
    if safety == SafetyDecision.refuse:
        return ConfidenceAssessment(
            confidence_state=ConfidenceState.unsafe,
            rationale_primary="safety_gate_refuse",
        )
    if safety == SafetyDecision.handoff:
        return ConfidenceAssessment(
            confidence_state=ConfidenceState.blocked,
            rationale_primary="safety_gate_handoff",
        )
    if safety == SafetyDecision.clarify_scope:
        return ConfidenceAssessment(
            confidence_state=ConfidenceState.ambiguous,
            rationale_primary="safety_gate_clarify_scope",
        )
    if model.ambiguity_flags:
        return ConfidenceAssessment(
            confidence_state=ConfidenceState.ambiguous,
            rationale_primary="model_ambiguity_flags",
            supporting_signals=list(model.ambiguity_flags),
        )
    if det.candidate_count >= 2 and det.top2_gap is not None:
        if det.top2_gap < CLOSE_TOP2_GAP:
            return ConfidenceAssessment(
                confidence_state=ConfidenceState.ambiguous,
                rationale_primary="close_candidates",
                supporting_signals=[f"top2_gap={det.top2_gap}"],
            )
    if det.alias_hits or det.top_match_score is not None:
        if det.top_match_score is None or det.top_match_score >= CLEAR_MODEL_CONFIDENCE:
            return ConfidenceAssessment(
                confidence_state=ConfidenceState.clear,
                rationale_primary="deterministic_match",
                supporting_signals=list(det.alias_hits),
            )
    if model.confidence >= CLEAR_MODEL_CONFIDENCE:
        return ConfidenceAssessment(
            confidence_state=ConfidenceState.clear,
            rationale_primary="model_confidence",
        )
    return ConfidenceAssessment(
        confidence_state=ConfidenceState.weak_but_usable,
        rationale_primary="no_positive_signal",
    )


def caps_reached(envelope: RequestEnvelope, now_ms: int) -> list[CapReached]:
    """Which budget lines are exhausted *right now* (CH02_02 step 3)."""
    b = envelope.execution_budget
    c = envelope.attempt_counters
    caps: list[CapReached] = []
    if c.total_loops >= b.max_total_loops:
        caps.append(CapReached.total_loop_cap)
    if c.reinterpretations >= b.max_reinterpretations:
        caps.append(CapReached.reinterpretation_cap)
    if c.clarification_turns >= b.max_clarification_turns:
        caps.append(CapReached.clarification_cap)
    if c.execution_retries >= b.max_execution_retries:
        caps.append(CapReached.execution_retry_cap)
    if c.model_escalations >= b.max_model_escalations:
        caps.append(CapReached.model_escalation_cap)
    return caps


def wall_clock_exceeded(envelope: RequestEnvelope, now_ms: int) -> bool:
    elapsed = (
        now_ms
        - envelope.timestamp_start_ms
        - envelope.execution_budget.wall_clock_paused_ms
    )
    return elapsed >= envelope.execution_budget.max_wall_clock_ms


def model_escalation_budget_left(envelope: RequestEnvelope) -> bool:
    c = envelope.attempt_counters
    b = envelope.execution_budget
    return c.model_escalations < b.max_model_escalations


def retry_budget_left(envelope: RequestEnvelope) -> bool:
    c = envelope.attempt_counters
    b = envelope.execution_budget
    return c.execution_retries < b.max_execution_retries


def routing_signals(
    envelope: RequestEnvelope,
    out: FrontHalfOutput,
    caps: list[CapReached],
) -> tuple[ConfidenceAssessment, RoutingSignals]:
    """The single place RoutingSignals is built, so tests and runtime can
    never drift on how the table inputs are derived."""
    assessment = confidence_of(
        out.safety, out.interpretation.deterministic, out.interpretation.model
    )
    det = out.interpretation.deterministic
    # calibration record only — no table row may condition on this (DP-9)
    assessment.aggregate_score = round(
        0.5 * out.interpretation.model.confidence
        + 0.5 * (det.top_match_score if det.top_match_score is not None else 0.0),
        3,
    )
    strong_evidence = (
        assessment.confidence_state == ConfidenceState.clear
        and det.candidate_count <= 1
        and (det.top_match_score if det.top_match_score is not None else 1.0)
        >= STRONG_TOP_MATCH
    )
    signals = RoutingSignals(
        safety=out.safety,
        risk=out.risk,
        action_type=out.action_type,
        confidence_state=assessment.confidence_state,
        missing_required_constraint=out.missing_required_constraint,
        user_resolvable_ambiguity=out.user_resolvable_ambiguity,
        strong_evidence=strong_evidence,
        any_budget_exhausted=CapReached.total_loop_cap in caps,
        model_escalation_budget_left=model_escalation_budget_left(envelope),
        aggregate_score=assessment.aggregate_score,
    )
    return assessment, signals


def budget_for_test(**overrides: int) -> ExecutionBudget:
    """Per-case budget override helper (golden cases and tests)."""
    return ExecutionBudget(**overrides)  # type: ignore[arg-type]
