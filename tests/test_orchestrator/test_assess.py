"""DP-9 in code: confidence is a *state* chosen by pattern, and budget caps
are plain counter comparisons."""

import pytest

from orchestrator import assess
from orchestrator.contracts import (
    AttemptCounters,
    CapReached,
    ConfidenceState,
    DeterministicSignals,
    ExecutionBudget,
    FrontHalfOutput,
    InterpretationRecord,
    ModelSignals,
    RequestEnvelope,
    SafetyDecision,
)


def _interp(**kw: object) -> InterpretationRecord:
    det = kw.pop("det", {})
    model = kw.pop("model", {})
    return InterpretationRecord(
        interpretation_id="int_test",
        request_id="req_test",
        timestamp_ms=0,
        normalized_query="q",
        task_type="test",
        deterministic=DeterministicSignals(**det),  # type: ignore[arg-type]
        model=ModelSignals(**model),  # type: ignore[arg-type]
    )


def _out(interp: InterpretationRecord, **kw: object) -> FrontHalfOutput:
    return FrontHalfOutput(interpretation=interp, **kw)  # type: ignore[arg-type]


class TestConfidenceOf:
    def test_safety_verdicts_short_circuit(self) -> None:
        for safety, state in [
            (SafetyDecision.refuse, ConfidenceState.unsafe),
            (SafetyDecision.handoff, ConfidenceState.blocked),
            (SafetyDecision.clarify_scope, ConfidenceState.ambiguous),
        ]:
            got = assess.confidence_of(safety, DeterministicSignals(), ModelSignals())
            assert got.confidence_state == state

    def test_ambiguity_flags_beat_positive_signals(self) -> None:
        got = assess.confidence_of(
            SafetyDecision.allow,
            DeterministicSignals(alias_hits=["春晖卡"]),
            ModelSignals(ambiguity_flags=["指代不明"]),
        )
        assert got.confidence_state == ConfidenceState.ambiguous

    def test_close_candidates_are_ambiguous(self) -> None:
        got = assess.confidence_of(
            SafetyDecision.allow,
            DeterministicSignals(candidate_count=2, top2_gap=0.05),
            ModelSignals(confidence=0.9),
        )
        assert got.confidence_state == ConfidenceState.ambiguous

    def test_alias_hit_is_clear(self) -> None:
        got = assess.confidence_of(
            SafetyDecision.allow,
            DeterministicSignals(alias_hits=["春晖卡"]),
            ModelSignals(),
        )
        assert got.confidence_state == ConfidenceState.clear

    def test_no_positive_signal_is_weak(self) -> None:
        got = assess.confidence_of(
            SafetyDecision.allow, DeterministicSignals(), ModelSignals(confidence=0.2)
        )
        assert got.confidence_state == ConfidenceState.weak_but_usable


class TestCaps:
    def envelope(self, **counters: int) -> RequestEnvelope:
        env = RequestEnvelope.new(text="q")
        env.attempt_counters = AttemptCounters(**counters)  # type: ignore[arg-type]
        return env

    def test_each_cap_fires_at_its_own_line(self) -> None:
        cases = {
            CapReached.total_loop_cap: {"total_loops": 6},
            CapReached.reinterpretation_cap: {"reinterpretations": 2},
            CapReached.clarification_cap: {"clarification_turns": 2},
            CapReached.execution_retry_cap: {"execution_retries": 2},
            CapReached.model_escalation_cap: {"model_escalations": 1},
        }
        for cap, counters in cases.items():
            assert cap in assess.caps_reached(self.envelope(**counters), 0), cap

    def test_fresh_envelope_has_no_caps(self) -> None:
        assert assess.caps_reached(self.envelope(), 0) == []

    def test_wall_clock(self) -> None:
        env = RequestEnvelope.new(text="q")
        assert assess.wall_clock_exceeded(env, env.timestamp_start_ms + 30_000)
        assert not assess.wall_clock_exceeded(env, env.timestamp_start_ms + 29_000)

    def test_budget_override_survives_roundtrip(self) -> None:
        env = RequestEnvelope.new(text="q", budget=ExecutionBudget(max_total_loops=2))
        assert assess.caps_reached(self.envelope_with(env, total_loops=2), 0) == [
            CapReached.total_loop_cap
        ]

    def envelope_with(self, env: RequestEnvelope, **counters: int) -> RequestEnvelope:
        env.attempt_counters = AttemptCounters(**counters)  # type: ignore[arg-type]
        return env


class TestRoutingSignals:
    def test_strong_evidence_needs_clear_and_top_match(self) -> None:
        env = RequestEnvelope.new(text="q")
        out = _out(
            _interp(
                det={"top_match_score": 0.9, "candidate_count": 1},
                model={"confidence": 0.9},
            )
        )
        _, signals = assess.routing_signals(env, out, [])
        assert signals.strong_evidence
        assert signals.confidence_state == ConfidenceState.clear

    def test_total_loop_cap_maps_to_budget_exhausted_only_for_r2(self) -> None:
        env = RequestEnvelope.new(text="q")
        out = _out(
            _interp(
                det={"top_match_score": 0.9, "candidate_count": 1},
                model={"confidence": 0.9},
            )
        )
        _, signals = assess.routing_signals(env, out, [CapReached.clarification_cap])
        assert not signals.any_budget_exhausted
        _, signals = assess.routing_signals(env, out, [CapReached.total_loop_cap])
        assert signals.any_budget_exhausted

    @pytest.mark.parametrize("left,expected", [(1, True), (0, False)])
    def test_model_escalation_budget(self, left: int, expected: bool) -> None:
        env = RequestEnvelope.new(text="q")
        env.attempt_counters.model_escalations = (
            env.execution_budget.max_model_escalations - left
        )
        out = _out(_interp())
        _, signals = assess.routing_signals(env, out, [])
        assert signals.model_escalation_budget_left is expected

    def test_aggregate_score_recorded_but_state_decides(self) -> None:
        env = RequestEnvelope.new(text="q")
        out = _out(
            _interp(
                det={"top_match_score": 0.9, "candidate_count": 1},
                model={"confidence": 0.9},
            )
        )
        assessment, signals = assess.routing_signals(env, out, [])
        assert assessment.aggregate_score > 0  # calibration record
        assert signals.aggregate_score == assessment.aggregate_score


class TestQuota:
    """05c: the quota signal is a level comparison over the envelope plus
    the one cross-request fact the runtime reads from the store."""

    def test_consumed_is_envelope_plus_other_requests_today(self) -> None:
        env = RequestEnvelope.new(
            text="q", budget=ExecutionBudget(max_llm_calls_per_day=5)
        )
        env.attempt_counters.llm_calls = 2
        assert not assess.quota_exhausted(env, 2)
        assert assess.quota_exhausted(env, 3)  # 2 + 3 == 5 -> at the ceiling

    def test_routing_signals_carries_the_flag_and_defaults_false(self) -> None:
        env = RequestEnvelope.new(text="q")
        out = _out(_interp())
        _, signals = assess.routing_signals(env, out, [])
        assert not signals.quota_exhausted
        _, signals = assess.routing_signals(env, out, [], quota_exhausted=True)
        assert signals.quota_exhausted
