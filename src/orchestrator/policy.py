"""The four decision tables as *data* with row ids, plus one first-match
interpreter (DP-7). No module here calls an LLM — every row is a pure
predicate over a signal object from ``contracts``.

Row order is deliberate and load-bearing (CH01: hard constraints first,
clarify before expensive reasoning, terminal default last). Each row carries
an id so ``tests`` can enumerate them and require >= 1 golden case fires each
— the design's claim that "a decision table is only as strong as the tests
covering its rows" becomes mechanically checkable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from .contracts import (
    CapReached,
    ConfidenceState,
    Decision,
    ExecutionSignals,
    RoutingSignals,
    ValidationSignals,
)

S = TypeVar("S")
Predicate = Callable[[S], bool]


@dataclass(frozen=True)
class Row(Generic[S]):
    row_id: str
    when: Predicate[S]
    action: Decision
    reason: str


class DecisionTable(Generic[S]):
    """Ordered rows; first fully-matching row wins. Every table ends in a
    catch-all so ``decide`` never returns None."""

    def __init__(self, name: str, rows: list[Row[S]]) -> None:
        self.name = name
        self.rows = rows

    def decide(self, signals: S) -> Row[S]:
        for row in self.rows:
            if row.when(signals):
                return row
        raise AssertionError(f"{self.name}: no row matched (missing catch-all?)")

    def row_ids(self) -> list[str]:
        return [r.row_id for r in self.rows]


# --- routing table (CH01, 9 rows) -----------------------------------------
ROUTING_TABLE: DecisionTable[RoutingSignals] = DecisionTable(
    "routing",
    [
        Row(
            "route_r1_reject_policy",
            lambda s: s.policy_block,
            Decision.reject,
            "policy_or_safety_block",
        ),
        Row(
            "route_r2_handoff_budget",
            lambda s: s.any_budget_exhausted,
            Decision.handoff_human,
            "escalation_budget_exhausted",
        ),
        Row(
            "route_r3_clarify_missing_constraint",
            lambda s: s.missing_required_constraint,
            Decision.clarify,
            "missing_required_constraint",
        ),
        Row(
            "route_r4_clarify_close_candidates",
            lambda s: s.user_resolvable_ambiguity and s.read_only_low_risk,
            Decision.clarify,
            "user_resolvable_ambiguity",
        ),
        Row(
            "route_r5_stronger_model_risky",
            lambda s: s.high_risk_or_write and not s.confidence_strong,
            Decision.stronger_model,
            "high_risk_low_confidence",
        ),
        Row(
            "route_r6_stronger_model_ambiguous",
            lambda s: s.confidence_state == ConfidenceState.ambiguous
            and s.model_escalation_budget_left,
            Decision.stronger_model,
            "flash_ambiguous",
        ),
        Row(
            "route_r7_proceed_strong",
            lambda s: s.strong_evidence and s.confidence_strong,
            Decision.proceed,
            "strong_evidence_clear",
        ),
        Row(
            "route_r8_proceed_conservative",
            lambda s: s.confidence_state == ConfidenceState.weak_but_usable
            and s.read_only_low_risk,
            Decision.proceed_conservative,
            "weak_but_usable_read_only",
        ),
        Row(
            "route_r9_handoff_default",
            lambda s: True,
            Decision.handoff_human,
            "unhandled_default",
        ),
    ],
)

# --- execution table (CH02_03, 7 rows) ------------------------------------
EXECUTION_TABLE: DecisionTable[ExecutionSignals] = DecisionTable(
    "execution",
    [
        Row(
            "exec_e1_reject_denied",
            lambda s: s.policy_denied,
            Decision.reject,
            "permission_or_policy_denied",
        ),
        Row(
            "exec_e2_handoff_retry_exhausted",
            lambda s: (not s.retry_budget_left) and (not s.has_alternate_capability),
            Decision.handoff_human,
            "retry_exhausted_no_alternate",
        ),
        Row(
            "exec_e3_retry_transient",
            lambda s: s.transient_failure and s.retry_budget_left,
            Decision.retry,
            "transient_backend_failure",
        ),
        Row(
            "exec_e4_switch_weak",
            lambda s: s.result_weak and s.has_alternate_capability,
            Decision.switch_capability,
            "weak_result_alternate_exists",
        ),
        Row(
            "exec_e5_clarify_user_constraint",
            lambda s: s.user_constraint_missing,
            Decision.clarify,
            "user_constraint_missing",
        ),
        Row(
            "exec_e6_proceed_grounded",
            lambda s: s.grounded_and_valid,
            Decision.accept,
            "grounded_and_valid",
        ),
        Row(
            "exec_e7_handoff_default",
            lambda s: True,
            Decision.handoff_human,
            "unhandled_default",
        ),
    ],
)

# --- validation table (CH02_03, 7 rows) -----------------------------------
VALIDATION_TABLE: DecisionTable[ValidationSignals] = DecisionTable(
    "validation",
    [
        Row(
            "val_v1_reject_policy",
            lambda s: s.policy_compliance_failed,
            Decision.reject,
            "policy_compliance_failed",
        ),
        Row(
            "val_v2_handoff_retry_exhausted",
            lambda s: (not s.retry_budget_left) and (not s.grounded_and_complete),
            Decision.handoff_human,
            "retry_exhausted_still_invalid",
        ),
        Row(
            "val_v3_switch_or_retry_grounding",
            lambda s: s.grounding_required_below_threshold,
            Decision.switch_capability,
            "grounding_below_threshold",
        ),
        Row(
            "val_v4_clarify_missing_fields",
            lambda s: s.missing_required_fields and s.user_can_supply_fields,
            Decision.clarify,
            "missing_required_fields_user",
        ),
        Row(
            "val_v5_accept",
            lambda s: s.grounded_and_complete,
            Decision.accept,
            "grounded_and_complete",
        ),
        Row(
            "val_v6_partial_answer",
            lambda s: s.partial_useful_low_risk,
            Decision.partial_answer,
            "partial_useful_low_risk",
        ),
        Row(
            "val_v7_handoff_default",
            lambda s: True,
            Decision.handoff_human,
            "unhandled_default",
        ),
    ],
)

ALL_TABLES: tuple[DecisionTable[Any], ...] = (
    ROUTING_TABLE,
    EXECUTION_TABLE,
    VALIDATION_TABLE,
)


# --- fallback table (CH02_02, 5 caps -> still-legal actions) --------------
@dataclass(frozen=True)
class FallbackRow:
    cap: CapReached
    allowed: frozenset[Decision]
    preferred: Decision


FALLBACK_ROWS: dict[CapReached, FallbackRow] = {
    cap: FallbackRow(cap, frozenset(allowed), preferred)
    for cap, (allowed, preferred) in {
        CapReached.reinterpretation_cap: (
            [Decision.clarify, Decision.stronger_model, Decision.handoff_human],
            Decision.clarify,
        ),
        CapReached.clarification_cap: (
            [Decision.proceed_conservative, Decision.handoff_human, Decision.reject],
            Decision.handoff_human,
        ),
        CapReached.execution_retry_cap: (
            [
                Decision.switch_capability,
                Decision.partial_answer,
                Decision.handoff_human,
                Decision.failed,
            ],
            Decision.switch_capability,
        ),
        CapReached.model_escalation_cap: (
            [Decision.clarify, Decision.handoff_human, Decision.reject],
            Decision.handoff_human,
        ),
        CapReached.total_loop_cap: (
            [
                Decision.accept,
                Decision.partial_answer,
                Decision.handoff_human,
                Decision.reject,
                Decision.failed,
            ],
            Decision.handoff_human,
        ),
    }.items()
}


def legal_after_cap(cap: CapReached) -> frozenset[Decision]:
    """Actions still permitted once ``cap`` is exhausted (CH02_02)."""
    return FALLBACK_ROWS[cap].allowed
