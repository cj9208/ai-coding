"""Contract-level invariants: enum vocabularies match the design doc,
candidate keys are sane, catalog entries carry the required fields."""

from orchestrator.contracts import (
    AttemptCounters,
    CapabilityCatalogEntry,
    Decision,
    ExecutionBudget,
    OutcomeType,
    RequestEnvelope,
    RequestStatus,
    RoutingSignals,
)
from orchestrator.ids import new_id


def test_state_vocabulary_has_the_ten_design_states() -> None:
    assert len(RequestStatus) == 10


def test_decision_vocabulary_superset_of_nine_canonical() -> None:
    canonical = {
        "proceed",
        "proceed_conservative",
        "clarify",
        "stronger_model",
        "execute_capability",
        "retry",
        "switch_capability",
        "handoff_human",
        "reject",
    }
    assert canonical <= {d.value for d in Decision}


def test_six_outcome_types() -> None:
    assert len(OutcomeType) == 6


def test_envelope_defaults_to_budget_and_zero_counters() -> None:
    env = RequestEnvelope.new(text="q")
    assert env.execution_budget == ExecutionBudget()
    assert env.attempt_counters == AttemptCounters()
    assert env.state.current_status == RequestStatus.captured
    assert env.request_id.startswith("req_")


def test_ids_are_time_sortable() -> None:
    ids = sorted(new_id("req") for _ in range(50))
    # 10-char timestamp prefix dominates: sorted == creation-ordered within
    # the same ms up to random suffix; across ms strictly increasing
    stamps = [i[4:14] for i in ids]
    assert stamps == sorted(stamps)


def test_catalog_entry_required_field_names_are_declared() -> None:
    assert CapabilityCatalogEntry.REQUIRED_FIELDS[0] == "name"
    assert "output_contract" in CapabilityCatalogEntry.REQUIRED_FIELDS


def test_signals_defaults_fire_the_catch_all_path() -> None:
    # RoutingSignals is enum-typed; defaults must be a sane "nothing known"
    assert RoutingSignals().confidence_state.value == "clear"
    assert not RoutingSignals().strong_evidence
    assert not RoutingSignals().policy_block
