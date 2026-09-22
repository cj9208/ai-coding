"""Runtime scenarios that the golden file can't express: budget survival
across process boundaries, the wall clock, the transition guard, handoff
packet content, and conservative-mode constraint propagation (DP-10)."""

from pathlib import Path
from typing import Callable, Iterator

import pytest

from orchestrator.capabilities.builtin import render_handoff_markdown
from orchestrator.capabilities.fake import EchoCapability, FakeFrontHalf
from orchestrator.contracts import (
    AttemptCounters,
    CapabilityCatalogEntry,
    ExecutionBudget,
    HandoffPacket,
    OutcomeType,
    OutputContract,
    RequestStatus,
)
from orchestrator.registry import HUMAN_HANDOFF, Registry
from orchestrator.runtime import LEGAL_TRANSITIONS, Orchestrator
from orchestrator.store import Store

STRONG = {
    "task_type": "test",
    "top_match_score": 0.9,
    "candidate_count": 1,
    "model": {"confidence": 0.9},
}
AMBIGUOUS = {
    "task_type": "test",
    "user_resolvable_ambiguity": True,
    "clarification_question": "指哪张卡？",
    "model": {"confidence": 0.2},
}


def make_registry(echo: EchoCapability | None = None) -> Registry:
    entry = CapabilityCatalogEntry(
        name="echo",
        owner="t",
        domain_scope="global",
        capability_version="0",
        rollout_status="active",
        use_when=["t"],
        avoid_when=[""],
        task_types_supported=["*"],
        tool_schema_bundle=["echo"],
        output_contract=OutputContract(required_fields=["text"]),
        fallbacks=[HUMAN_HANDOFF],
    )
    handoff = entry.model_copy(update={"name": HUMAN_HANDOFF})
    return Registry(
        entries={"echo": entry, HUMAN_HANDOFF: handoff},
        impls={"echo": echo or EchoCapability()},
    )


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "orch.db")
    yield s
    s.close()


class TestTurnFlow:
    def test_clarification_survives_the_process_boundary(self, store: Store) -> None:
        """DP-8: resume with a fresh Orchestrator instance (simulated new
        process) still sees spent budget on the envelope."""
        orch1 = Orchestrator(store, make_registry(), FakeFrontHalf([AMBIGUOUS]))
        first = orch1.run_turn("那个卡")
        assert first.status == RequestStatus.awaiting_clarification
        assert first.question == "指哪张卡？"

        orch2 = Orchestrator(store, make_registry(), FakeFrontHalf([STRONG]))
        second = orch2.resume(first.request_id, "春晖卡")
        assert second.status == RequestStatus.completed
        assert second.outcome_type == OutcomeType.answered

        env = store.get_request(first.request_id)
        assert env is not None
        assert env.attempt_counters == AttemptCounters(
            total_loops=2, clarification_turns=1, tool_calls=1
        )

    def test_a_request_resumed_elsewhere_cannot_be_resumed_again(
        self, store: Store
    ) -> None:
        """A second process that read the request before the first one
        finished is refused at resume's status gate; the interleaved-write
        case (both read while still awaiting, then both commit) is refused
        one layer down by the version CAS — see
        ``test_store.py::test_update_request_is_a_compare_and_set``."""
        orch1 = Orchestrator(store, make_registry(), FakeFrontHalf([AMBIGUOUS]))
        first = orch1.run_turn("那个卡")
        assert first.status == RequestStatus.awaiting_clarification

        winner = Orchestrator(store, make_registry(), FakeFrontHalf([STRONG]))
        loser = Orchestrator(store, make_registry(), FakeFrontHalf([STRONG]))
        assert (
            winner.resume(first.request_id, "春晖卡").status == RequestStatus.completed
        )
        with pytest.raises(ValueError, match="not awaiting"):
            loser.resume(first.request_id, "春晖卡")

        # the winner's counters survived: nothing was silently overwritten
        env = store.get_request(first.request_id)
        assert env is not None
        assert env.attempt_counters.clarification_turns == 1

    def test_object_seq_comes_from_the_envelope_counter(self, store: Store) -> None:
        """DP-8: the seq lives on the envelope, so no read-then-write
        MAX(seq)+1 race; replay ordering is unchanged."""
        orch = Orchestrator(store, make_registry(), FakeFrontHalf([STRONG]))
        done = orch.run_turn("q")
        env = store.get_request(done.request_id)
        assert env is not None
        seqs = [o["seq"] for o in store.objects(done.request_id)]
        assert seqs == list(range(1, len(seqs) + 1))
        assert env.state.next_seq == len(seqs) + 1

    def test_resume_rejects_non_awaiting_request(self, store: Store) -> None:
        orch = Orchestrator(store, make_registry(), FakeFrontHalf([STRONG]))
        done = orch.run_turn("q")
        with pytest.raises(ValueError, match="not awaiting"):
            orch.resume(done.request_id, "answer")

    def test_wall_clock_kills_a_stalled_request(self, store: Store) -> None:
        import time

        start = int(time.time() * 1000)
        clock: Callable[[], int] = lambda: start + 40_000
        orch = Orchestrator(
            store, make_registry(), FakeFrontHalf([STRONG]), now_ms=clock
        )
        result = orch.run_turn("q")
        assert result.status == RequestStatus.failed
        assert store.events(result.request_id)[0]["event"] == "request_captured"

    def test_human_answer_time_is_not_charged_to_the_wall_clock(
        self, store: Store
    ) -> None:
        """DP-4's wall clock budgets machine work; a request parked on a
        human for an hour must still complete on resume."""
        import time

        start = int(time.time() * 1000)
        t = {"now": start}
        orch = Orchestrator(
            store,
            make_registry(),
            FakeFrontHalf([AMBIGUOUS, STRONG]),
            now_ms=lambda: t["now"],
        )
        first = orch.run_turn("那个卡")
        assert first.status == RequestStatus.awaiting_clarification
        t["now"] = start + 3_600_000  # the human answers an hour later
        second = orch.resume(first.request_id, "春晖卡")
        assert second.status == RequestStatus.completed

        env = store.get_request(first.request_id)
        assert env is not None
        assert env.execution_budget.wall_clock_paused_ms >= 3_600_000 - 1_000
        assert env.state.wait_started_ms is None

    def test_resume_past_the_ttl_restarts_from_a_clean_machine_budget(
        self, store: Store
    ) -> None:
        """DP-4 makes a parked request resumable forever; past the TTL the
        interpretation it would continue is stale, so the resumed turn gets
        fresh machine counters while the lifecycle bound survives."""
        import time

        from orchestrator.config import Lifecycle

        start = int(time.time() * 1000)
        t = {"now": start}
        orch = Orchestrator(
            store,
            make_registry(),
            FakeFrontHalf([AMBIGUOUS, STRONG]),
            now_ms=lambda: t["now"],
        )
        first = orch.run_turn("那个卡")
        assert first.status == RequestStatus.awaiting_clarification
        t["now"] = start + Lifecycle.CLARIFICATION_TTL_MS + 1_000  # months later
        second = orch.resume(first.request_id, "春晖卡")
        assert second.status == RequestStatus.completed

        env = store.get_request(first.request_id)
        assert env is not None
        assert env.attempt_counters == AttemptCounters(
            total_loops=1, clarification_turns=1, tool_calls=1
        )
        events = [e["event"] for e in store.events(first.request_id)]
        assert "clarification_expired" in events

    def test_illegal_transition_is_refused(self, store: Store) -> None:
        from orchestrator.contracts import RequestEnvelope

        orch = Orchestrator(store, make_registry(), FakeFrontHalf([STRONG]))
        env = RequestEnvelope.new(text="q")
        store.create_request(env)
        with pytest.raises(AssertionError, match="illegal transition"):
            orch._transition(env, RequestStatus.completed)

    def test_conservative_constraints_reach_the_capability(self, store: Store) -> None:
        echo = EchoCapability()
        weak = {"task_type": "test", "model": {"confidence": 0.1}}
        orch = Orchestrator(store, make_registry(echo), FakeFrontHalf([weak]))
        result = orch.run_turn("省钱卡大概有什么")
        assert result.status == RequestStatus.completed
        ctx = echo.contexts[0]
        assert ctx.constraints["conservative"] is True
        assert ctx.constraints["topk_factor"] == pytest.approx(1.5)

    def test_execution_record_written_on_failure(self, store: Store) -> None:
        from orchestrator.capabilities.fake import ScriptedCapability

        script = ScriptedCapability(
            [{"status": "failed", "code": "permission_denied", "output": {}}]
        )
        entry = CapabilityCatalogEntry(
            name="echo",
            owner="t",
            domain_scope="global",
            capability_version="0",
            rollout_status="active",
            use_when=["t"],
            avoid_when=[""],
            task_types_supported=["*"],
            tool_schema_bundle=[],
            output_contract=OutputContract(required_fields=["text"]),
            fallbacks=[],
        )
        registry = Registry(entries={"echo": entry}, impls={"echo": script})
        orch = Orchestrator(store, registry, FakeFrontHalf([STRONG]))
        result = orch.run_turn("导出全部数据")
        assert result.status == RequestStatus.rejected
        executions = [
            o for o in store.objects(result.request_id) if o["kind"] == "execution"
        ]
        assert executions[0]["payload"]["policy_check"]["allowed"] is False


class TestHandoff:
    def test_packet_has_six_sections_and_survives_export(self, store: Store) -> None:
        budget = ExecutionBudget(max_model_escalations=0)
        orch = Orchestrator(store, make_registry(), FakeFrontHalf([AMBIGUOUS_2]))
        result = orch.run_turn("那个东西", budget=budget)
        assert result.status == RequestStatus.handoff
        assert result.handoff_id
        rows = store.objects_of_kind("handoff")
        packet = HandoffPacket.model_validate(rows[0]["payload"])
        md = render_handoff_markdown(packet)
        for section in range(1, 7):
            assert f"## {section}." in md
        assert packet.reason.code
        assert packet.conversation_context.original_input == "那个东西"

    def test_events_end_with_human_handoff_created(self, store: Store) -> None:
        budget = ExecutionBudget(max_model_escalations=0)
        orch = Orchestrator(store, make_registry(), FakeFrontHalf([AMBIGUOUS_2]))
        result = orch.run_turn("那个东西", budget=budget)
        events = [e["event"] for e in store.events(result.request_id)]
        assert events[-1] == "human_handoff_created"


AMBIGUOUS_2 = {
    "task_type": "test",
    "model": {"confidence": 0.1, "ambiguity_flags": ["指代不明"]},
}


def test_transition_table_terminals_have_no_exits() -> None:
    for terminal in (
        RequestStatus.completed,
        RequestStatus.handoff,
        RequestStatus.rejected,
        RequestStatus.failed,
    ):
        assert LEGAL_TRANSITIONS[terminal] == frozenset()
