"""Every row of every decision table must be firable — the DP-7 contract:
rows are named, so a parametrized test can demand >= 1 case per row_id.
Signals are built directly here; golden cases cover the end-to-end paths."""

import pytest

from orchestrator import policy
from orchestrator.contracts import (
    ActionType,
    CapReached,
    ConfidenceState,
    Decision,
    ExecutionSignals,
    RiskLevel,
    RoutingSignals,
    SafetyDecision,
    ValidationSignals,
)

ROUTING_FIRINGS: dict[str, RoutingSignals] = {
    "route_r1_reject_policy": RoutingSignals(policy_violation=True),
    "route_r2_handoff_budget": RoutingSignals(any_budget_exhausted=True),
    "route_r10_quota_exhausted": RoutingSignals(quota_exhausted=True),
    "route_r3_clarify_missing_constraint": RoutingSignals(
        missing_required_constraint=True
    ),
    "route_r4_clarify_close_candidates": RoutingSignals(user_resolvable_ambiguity=True),
    "route_r5_stronger_model_risky": RoutingSignals(
        risk=RiskLevel.high, confidence_state=ConfidenceState.ambiguous
    ),
    "route_r6_stronger_model_ambiguous": RoutingSignals(
        confidence_state=ConfidenceState.ambiguous
    ),
    "route_r7_proceed_strong": RoutingSignals(
        confidence_state=ConfidenceState.clear, strong_evidence=True
    ),
    "route_r8_proceed_conservative": RoutingSignals(
        confidence_state=ConfidenceState.weak_but_usable
    ),
    # defaults: clear but no strong evidence -> nothing matched before r9
    "route_r9_handoff_default": RoutingSignals(
        confidence_state=ConfidenceState.blocked
    ),
}

EXECUTION_FIRINGS: dict[str, ExecutionSignals] = {
    "exec_e1_reject_denied": ExecutionSignals(policy_denied=True),
    "exec_e2_handoff_retry_exhausted": ExecutionSignals(retry_budget_left=False),
    "exec_e3_retry_transient": ExecutionSignals(transient_failure=True),
    "exec_e4_switch_weak": ExecutionSignals(
        result_weak=True, has_alternate_capability=True
    ),
    "exec_e5_clarify_user_constraint": ExecutionSignals(user_constraint_missing=True),
    "exec_e6_proceed_grounded": ExecutionSignals(grounded_and_valid=True),
    "exec_e7_handoff_default": ExecutionSignals(),
}

VALIDATION_FIRINGS: dict[str, ValidationSignals] = {
    "val_v1_reject_policy": ValidationSignals(policy_compliance_failed=True),
    "val_v2_handoff_retry_exhausted": ValidationSignals(retry_budget_left=False),
    "val_v3_switch_or_retry_grounding": ValidationSignals(
        grounding_required_below_threshold=True
    ),
    "val_v4_clarify_missing_fields": ValidationSignals(
        missing_required_fields=True, user_can_supply_fields=True
    ),
    "val_v5_accept": ValidationSignals(grounded_and_complete=True),
    "val_v6_partial_answer": ValidationSignals(partial_useful_low_risk=True),
    "val_v7_handoff_default": ValidationSignals(),
}


@pytest.mark.parametrize("row_id", policy.ROUTING_TABLE.row_ids())
def test_every_routing_row_fires(row_id: str) -> None:
    signals = ROUTING_FIRINGS[row_id]
    assert policy.ROUTING_TABLE.decide(signals).row_id == row_id


@pytest.mark.parametrize("row_id", policy.EXECUTION_TABLE.row_ids())
def test_every_execution_row_fires(row_id: str) -> None:
    signals = EXECUTION_FIRINGS[row_id]
    assert policy.EXECUTION_TABLE.decide(signals).row_id == row_id


@pytest.mark.parametrize("row_id", policy.VALIDATION_TABLE.row_ids())
def test_every_validation_row_fires(row_id: str) -> None:
    signals = VALIDATION_FIRINGS[row_id]
    assert policy.VALIDATION_TABLE.decide(signals).row_id == row_id


def test_row_order_is_hard_constraint_first() -> None:
    ids = policy.ROUTING_TABLE.row_ids()
    assert ids[0] == "route_r1_reject_policy"
    assert ids[1] == "route_r2_handoff_budget"
    assert ids[-1] == "route_r9_handoff_default"


def test_quota_row_sits_behind_loop_cap_ahead_of_every_spend_row() -> None:
    # a request that overspent its loops is the more local failure (r2);
    # a user out of daily calls must not reach clarify or escalate (r3+)
    both = RoutingSignals(any_budget_exhausted=True, quota_exhausted=True)
    assert policy.ROUTING_TABLE.decide(both).row_id == "route_r2_handoff_budget"
    ahead = RoutingSignals(quota_exhausted=True, missing_required_constraint=True)
    assert policy.ROUTING_TABLE.decide(ahead).row_id == "route_r10_quota_exhausted"


def test_safety_refuse_blocks_even_with_strong_evidence() -> None:
    signals = RoutingSignals(
        safety=SafetyDecision.refuse,
        strong_evidence=True,
        confidence_state=ConfidenceState.clear,
    )
    assert policy.ROUTING_TABLE.decide(signals).action == Decision.reject


def test_write_action_needs_clear_confidence() -> None:
    signals = RoutingSignals(
        action_type=ActionType.write,
        strong_evidence=True,
        confidence_state=ConfidenceState.clear,
    )
    # strong + clear => r7 proceed (write-specific gating is the execution
    # policy check's job, CH02_03; the routing table stays 10 rows)
    assert policy.ROUTING_TABLE.decide(signals).action == Decision.proceed


@pytest.mark.parametrize("cap", list(CapReached))
def test_fallback_rows_always_allow_handoff_and_are_nonempty(cap: CapReached) -> None:
    allowed = policy.legal_after_cap(cap)
    assert Decision.handoff_human in allowed
    assert allowed
    assert policy.FALLBACK_ROWS[cap].preferred in allowed


def test_fallback_covers_every_cap_row_id_set() -> None:
    assert set(policy.FALLBACK_ROWS) == set(CapReached)
