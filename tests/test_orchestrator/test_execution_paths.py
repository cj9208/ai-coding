"""M3 proof: the retry/switch rows of the execution + validation tables run
against a two-capability registry (rag_query -> structured_lookup) with both
real adapters — zero LLM, monkeypatched rag/lookup seams.

This is the milestone's acceptance line: the runtime gained no branch for
lookup; the tables reached it purely through the declared fallback.
"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from file_manager.search.base import SearchHit, SearchResult
from orchestrator.capabilities import rag as rag_cap
from orchestrator.capabilities.lookup import StructuredLookupCapability
from orchestrator.capabilities.rag import RagQueryCapability
from orchestrator.contracts import (
    CapabilityCatalogEntry,
    CapabilityResult,
    OutputContract,
    ResultStatus,
)
from orchestrator.registry import Registry
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store
from rag.contract import Answer, Chunk, ChunkType, Claim, EvidencePack, Outcome


def _hit(i: int = 7) -> SearchHit:
    return SearchHit(
        id=i,
        filename=f"invoice_{i}.pdf",
        title="差旅发票",
        project_id=2,
        project_name="财务",
        uploader_name="张三",
        team_name="财务组",
        extension="pdf",
        size=1024,
        created_at=datetime(2026, 9, 1),
        matched=["标题"],
    )


class _FakeClient:
    @contextmanager
    def session(self) -> Iterator[Any]:
        yield object()


class _StubCapability:
    """Reports a crash first, then a lookup-shaped success (retry tests)."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def run(self, ctx: Any) -> CapabilityResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("backend exploded")
        return CapabilityResult(
            status=ResultStatus.success,
            output={"answer_markdown": "重试后找到记录", "records": []},
            tool_steps=["lookup.metadata"],
        )


def _rag_entry(fallbacks: list[str]) -> CapabilityCatalogEntry:
    return CapabilityCatalogEntry(
        name="rag_query",
        owner="platform",
        domain_scope="knowledge",
        capability_version="0.1.0",
        rollout_status="active",
        task_types_supported=["*"],
        use_when=["q"],
        avoid_when=["nothing"],
        tool_schema_bundle=["rag.retrieve"],
        output_contract=OutputContract(
            required_fields=["answer_markdown", "citations"]
        ),
        fallbacks=fallbacks,
        validation_rules=["grounding_coverage_min"],
    )


def _lookup_entry() -> CapabilityCatalogEntry:
    return CapabilityCatalogEntry(
        name="structured_lookup",
        owner="platform",
        domain_scope="records",
        capability_version="0.1.0",
        rollout_status="active",
        task_types_supported=["structured_lookup_only"],
        use_when=["records"],
        avoid_when=["nothing"],
        tool_schema_bundle=["file_manager.search.metadata"],
        output_contract=OutputContract(required_fields=["answer_markdown", "records"]),
        fallbacks=["human_handoff"],
        validation_rules=[],
    )


def _pack() -> EvidencePack:
    return EvidencePack(
        query="q",
        chunks=[
            Chunk(
                chunk_id="c1",
                doc_id="doc1.pdf",
                chunk_type=ChunkType.prose,
                is_parent=False,
                parent_chunk_id=None,
                section_path="1 总则",
                text="片段",
                page_span=(0, 0),
                content_hash="h1",
            )
        ],
    )


def _orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: Answer,
    *,
    lookup_result: SearchResult | None = None,
    lookup_impl: Any = None,
    rag_fallbacks: list[str] | None = None,
):
    async def fake_generate(pack: Any, question: str, client: Any = None) -> Answer:
        return answer

    monkeypatch.setattr(rag_cap, "retrieve", lambda *a, **kw: _pack())
    monkeypatch.setattr(rag_cap, "generate", fake_generate)
    monkeypatch.setattr(rag_cap, "RagStore", lambda path: object())

    if lookup_impl is None:
        backend_result = lookup_result or SearchResult(hits=[_hit()], total=1)

        class _Backend:
            def search(self, db: Any, query: Any) -> SearchResult:
                return backend_result

        lookup_impl = StructuredLookupCapability(
            client=_FakeClient(), backend=_Backend()
        )

    entries = [_rag_entry(rag_fallbacks or ["structured_lookup", "human_handoff"])]
    entries.append(_lookup_entry())
    impls: dict[str, Any] = {
        "rag_query": RagQueryCapability(),
        "structured_lookup": lookup_impl,
    }
    reg = Registry(entries={e.name: e for e in entries}, impls=impls)

    from orchestrator.capabilities.fake import FakeFrontHalf

    strong = {
        "task_type": "faq_howto",
        "top_match_score": 0.9,
        "candidate_count": 1,
        "model": {"confidence": 0.9},
    }
    store = Store(tmp_path / "exec.db")
    return Orchestrator(store, reg, FakeFrontHalf([strong]))


# -- e4: weak rag result switches to the declared fallback ----------------------
def test_e4_switches_from_insufficient_rag_to_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = _orchestrator(
        tmp_path,
        monkeypatch,
        Answer(outcome=Outcome.insufficient, notes="证据不足"),
    )
    result = orch.run_turn("去年的差旅发票归档在哪")
    assert result.status.value == "completed"
    assert "差旅发票" in result.response

    recs = [
        o for o in orch.store.objects(result.request_id) if o["kind"] == "execution"
    ]
    assert [r["payload"]["capability_name"] for r in recs] == [
        "rag_query",
        "structured_lookup",
    ]


# -- v3: under-grounded answer fails validation, then switches ------------------
def test_v3_switches_after_low_grounding_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = _orchestrator(
        tmp_path,
        monkeypatch,
        Answer(
            outcome=Outcome.answered,
            text="一句没有任何引用支撑的回答。",
            claims=[Claim(text="无支撑", refs=[])],
        ),
    )
    result = orch.run_turn("差旅报销上限")
    assert result.status.value == "completed"
    assert "共找到 1 条" in result.response  # lookup's answer, not rag's

    events = orch.store.events(result.request_id)
    rows = [e for e in events if e["event"] == "validation_completed"]
    assert rows[0]["payload"]["row_id"] == "val_v3_switch_or_retry_grounding"
    assert rows[-1]["payload"]["action"] == "accept"


# -- e5 still wins over e4: a missing user constraint is not switchable --------
def test_clarify_from_capability_survives_a_real_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = _orchestrator(
        tmp_path,
        monkeypatch,
        Answer(outcome=Outcome.clarify, clarification="您要查哪个年度的发票？"),
    )
    result = orch.run_turn("差旅发票")
    assert result.status.value == "awaiting_clarification"
    assert result.question == "您要查哪个年度的发票？"


# -- e3: a transient failure retries the same capability -------------------------
def test_e3_retries_transient_failure_then_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = _StubCapability(fail_times=1)
    orch = _orchestrator(
        tmp_path,
        monkeypatch,
        Answer(outcome=Outcome.insufficient),
        lookup_impl=flaky,
    )
    # route straight to lookup: rag's weak result switches there, the stub
    # then crashes once (-> capability_crashed -> transient) and succeeds
    result = orch.run_turn("差旅发票在哪")
    assert result.status.value == "completed"
    assert "重试后找到记录" in result.response
    assert flaky.calls == 2
    envelope = orch.store.get_request(result.request_id)
    assert envelope is not None
    assert envelope.attempt_counters.execution_retries == 1


def test_e2_hands_off_when_retry_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_fails = _StubCapability(fail_times=99)
    orch = _orchestrator(
        tmp_path,
        monkeypatch,
        Answer(outcome=Outcome.insufficient),
        lookup_impl=always_fails,
    )
    result = orch.run_turn("差旅发票在哪")
    assert result.status.value == "handoff"
    assert result.handoff_id
    envelope = orch.store.get_request(result.request_id)
    assert envelope is not None
    assert envelope.attempt_counters.execution_retries == 2  # Budget.MAX = 2
