from __future__ import annotations

from research_agent.contracts.models import (
    EvidencePack,
    Finding,
    ResearchBrief,
)


class TestStoreBasics:
    def test_transition_is_atomic(self, store):
        store.create_session("s1")
        pack = EvidencePack(session_id="s1", stop_reason="budget")
        store.set_budget("s1", "collect_calls", 30)
        store.transition(
            "s1",
            "CLARIFY",
            artifacts=[pack],
            bumps={"collect_calls": 7},
            stop_reason="budget",
        )
        assert store.get_phase("s1") == "CLARIFY"
        assert store.get_stop_reason("s1") == "budget"
        assert store.budget_used("s1", "collect_calls") == 7
        assert store.latest_artifact("s1", "EvidencePack")["stop_reason"] == "budget"

    def test_stop_reason_sentinel(self, store):
        store.create_session("s1")
        store.transition("s1", "REFLECT", stop_reason="budget")
        store.transition("s1", "CLARIFY")  # False default: leave unchanged
        assert store.get_stop_reason("s1") == "budget"

    def test_findings_and_captures_scoped_per_session(self, store):
        store.create_session("a")
        store.create_session("b")
        c1, dup_a = store.add_capture("a", url="u1", text="t1", adapter="x")
        assert not dup_a and c1 == "C1"
        c1b, _ = store.add_capture("b", url="other", text="other", adapter="x")
        assert c1b == "C1"  # ids restart per session; no PK collision
        dup, duped = store.add_capture("a", url="u1", text="t1", adapter="x")
        assert duped and dup == "C1"

    def test_allocate_finding_ids(self, store):
        store.create_session("s1")
        assert store.allocate_finding_ids("s1", 3) == ["F1", "F2", "F3"]
        f = Finding(id="F1", capture_id="C1", claim="x", quote="x")
        store.add_findings("s1", [f])
        assert store.allocate_finding_ids("s1", 2) == ["F2", "F3"]
        assert len(store.findings("s1")) == 1

    def test_artifact_history_append_only(self, store):
        store.create_session("s1")
        store.append_artifact("s1", ResearchBrief(topic="a"))
        store.append_artifact("s1", ResearchBrief(topic="b"))
        arts = store.artifacts("s1", "ResearchBrief")
        assert [a["topic"] for a in arts] == ["a", "b"]  # full history kept
        # session_id + created_at stamped on write
        assert arts[0]["session_id"] == "s1" and arts[0]["created_at"]

    def test_gap_event_folds(self, store):
        store.create_session("s1")
        store.gap_event("s1", "preference", "pg1", "open")
        assert store.open_gaps("s1", "preference") == ["pg1"]
        store.gap_event("s1", "preference", "pg1", "asked", ref="ques_1")
        assert store.open_gaps("s1", "preference") == []
