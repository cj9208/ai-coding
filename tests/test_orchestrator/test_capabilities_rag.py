"""M2 rag adapter: outcome mapping, DP-10 conservative widening, grounding
coverage — all against monkeypatched rag functions (zero LLM, zero corpus).
"""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from orchestrator.capabilities import rag as rag_cap
from orchestrator.capabilities.fake import FakeFrontHalf, ScriptedCapability
from orchestrator.capabilities.rag import RagQueryCapability
from orchestrator.contracts import (
    CapabilityCatalogEntry,
    CapabilityContext,
    OutputContract,
    ResultStatus,
)
from orchestrator.registry import Registry
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store
from rag import answer as rag_answer
from rag.contract import Answer, Chunk, ChunkType, Claim, EvidencePack, Outcome


def _chunk(i: int = 1) -> Chunk:
    return Chunk(
        chunk_id=f"c{i}",
        doc_id=f"doc{i}.pdf",
        chunk_type=ChunkType.prose,
        is_parent=False,
        parent_chunk_id=None,
        section_path="1 总则",
        text=f"片段 {i}",
        page_span=(i - 1, i - 1),
        content_hash=f"h{i}",
    )


def _pack(n: int = 2) -> EvidencePack:
    return EvidencePack(query="q", chunks=[_chunk(i) for i in range(1, n + 1)])


def _ctx(constraints: dict[str, Any] | None = None) -> CapabilityContext:
    return CapabilityContext(
        request_id="req_1",
        session_id="sess_1",
        normalized_query="年假怎么休",
        task_type="faq_howto",
        constraints=constraints or {},
    )


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patches the adapter's whole rag seam; tests set ``answer``/``pack``."""
    state: dict[str, Any] = {"pack": _pack(), "answer": None, "retrieve_kwargs": {}}

    def fake_retrieve(store: Any, question: str, k: int = 5, **kw: Any) -> EvidencePack:
        state["retrieve_kwargs"] = {"k": k, "question": question}
        return state["pack"]

    async def fake_generate(
        pack: EvidencePack, question: str, client: Any = None
    ) -> Answer:
        assert state["answer"] is not None, "test must set an Answer"
        return state["answer"]

    monkeypatch.setattr(rag_cap, "retrieve", fake_retrieve)
    monkeypatch.setattr(rag_cap, "generate", fake_generate)
    monkeypatch.setattr(rag_cap, "RagStore", lambda path: object())
    return state


# -- outcome translation ------------------------------------------------------
async def test_answered_maps_to_success_with_citations(patched: dict) -> None:
    patched["answer"] = Answer(
        outcome=Outcome.answered,
        text="年假 5 天[1]，需提前申请[2]。",
        claims=[Claim(text="年假 5 天", refs=[1]), Claim(text="需提前申请", refs=[2])],
    )
    result = await RagQueryCapability().run(_ctx())
    assert result.status == ResultStatus.success
    assert result.confidence_signals["grounding_coverage"] == 1.0
    anchors = [c["anchor"] for c in result.output["citations"]]
    assert anchors == [1, 2]
    assert result.evidence_refs == ["doc1.pdf#p1", "doc2.pdf#p2"]


async def test_partial_withholds_citations_and_scores_coverage(patched: dict) -> None:
    patched["answer"] = Answer(
        outcome=Outcome.partial,
        text="部分回答[1]，未证实的部分。",
        claims=[Claim(text="部分回答", refs=[1]), Claim(text="未证实", refs=[])],
    )
    result = await RagQueryCapability().run(_ctx())
    assert result.status == ResultStatus.success
    assert "citations" not in result.output  # required-field gap -> v6 path
    assert result.confidence_signals["grounding_coverage"] == 0.5


async def test_clarify_becomes_user_constraint_missing(patched: dict) -> None:
    patched["answer"] = Answer(
        outcome=Outcome.clarify, text="", clarification="你想问哪类假？"
    )
    result = await RagQueryCapability().run(_ctx())
    assert result.status == ResultStatus.weak
    assert result.code == "user_constraint_missing"
    assert result.output["clarification"] == "你想问哪类假？"


async def test_insufficient_becomes_weak_evidence(patched: dict) -> None:
    patched["answer"] = Answer(outcome=Outcome.insufficient, notes="evidence too weak")
    result = await RagQueryCapability().run(_ctx())
    assert result.status == ResultStatus.weak
    assert result.code == "insufficient_evidence"


async def test_retrieve_crash_is_structured_failure(
    patched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **kw: Any) -> EvidencePack:
        raise RuntimeError("no such table: chunks")

    monkeypatch.setattr(rag_cap, "retrieve", boom)
    result = await RagQueryCapability().run(_ctx())
    assert result.status == ResultStatus.failed
    assert result.code == "dependency_unavailable"


# -- DP-10: the conservative constraint lands on retrieval width --------------
async def test_default_k(patched: dict) -> None:
    patched["answer"] = Answer(
        outcome=Outcome.answered, claims=[Claim(text="a", refs=[1])]
    )
    await RagQueryCapability().run(_ctx())
    assert patched["retrieve_kwargs"]["k"] == 5


async def test_conservative_widens_k_by_factor(patched: dict) -> None:
    patched["answer"] = Answer(
        outcome=Outcome.answered, claims=[Claim(text="a", refs=[1])]
    )
    await RagQueryCapability().run(_ctx({"conservative": True, "topk_factor": 1.5}))
    assert patched["retrieve_kwargs"]["k"] == 8  # ceil(5 * 1.5)


async def test_capability_never_sees_the_envelope(patched: dict) -> None:
    patched["answer"] = Answer(
        outcome=Outcome.answered, claims=[Claim(text="a", refs=[1])]
    )
    ctx = _ctx()
    await RagQueryCapability().run(ctx)
    assert patched["retrieve_kwargs"]["question"] == "年假怎么休"
    assert ctx.normalized_query == "年假怎么休"  # untouched by the adapter


# -- through the real runtime: rag answers / asks / goes partial --------------
def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer):
    from orchestrator.capabilities.fake import FakeFrontHalf
    from orchestrator.contracts import CapabilityCatalogEntry, OutputContract
    from orchestrator.registry import Registry
    from orchestrator.runtime import Orchestrator
    from orchestrator.store import Store

    def fake_retrieve(store: Any, question: str, k: int = 5, **kw: Any) -> EvidencePack:
        return _pack()

    async def fake_generate(
        pack: EvidencePack, question: str, client: Any = None
    ) -> Answer:
        return answer

    monkeypatch.setattr(rag_cap, "retrieve", fake_retrieve)
    monkeypatch.setattr(rag_cap, "generate", fake_generate)
    monkeypatch.setattr(rag_cap, "RagStore", lambda path: object())
    entry = CapabilityCatalogEntry(
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
        fallbacks=["human_handoff"],
        validation_rules=["grounding_coverage_min"],
    )
    reg = Registry(
        entries={entry.name: entry},
        impls={entry.name: RagQueryCapability()},
    )
    strong = {
        "task_type": "faq_howto",
        "top_match_score": 0.9,
        "candidate_count": 1,
        "model": {"confidence": 0.9},
    }
    store = Store(tmp_path / "rag.db")
    return Orchestrator(store, reg, FakeFrontHalf([strong]))


def test_runtime_completed_with_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _runtime(
        tmp_path,
        monkeypatch,
        Answer(
            outcome=Outcome.answered,
            text="按制度[1]。",
            claims=[Claim(text="按制度", refs=[1])],
        ),
    ).run_turn("年假怎么休")
    assert result.status.value == "completed"
    assert "按制度" in result.response


def test_runtime_partial_answer_when_citations_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _runtime(
        tmp_path,
        monkeypatch,
        Answer(
            outcome=Outcome.partial,
            text="一半有据[1]，一半没有。",
            claims=[Claim(text="一半有据", refs=[1]), Claim(text="一半没有", refs=[])],
        ),
    ).run_turn("年假怎么休")
    assert result.status.value == "completed"
    assert result.outcome_type.value == "partial_answer"


def test_runtime_clarifies_from_the_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _runtime(
        tmp_path,
        monkeypatch,
        Answer(outcome=Outcome.clarify, clarification="你想问哪类假？"),
    ).run_turn("假怎么休")
    assert result.status.value == "awaiting_clarification"
    assert result.question == "你想问哪类假？"


class _RawJsonClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    async def chat_json(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        schema: Any = None,
        model: Any = None,
        temperature: Any = None,
    ) -> dict[str, Any]:
        self.calls += 1
        assert "Evidence:" in prompt
        assert "[1]" in prompt
        assert "grounded answering module" in system_prompt
        assert temperature == 0.2
        return deepcopy(self.payload)


def _raw_claims_payload(
    total: int,
    cited: int,
    *,
    outcome: str = "answered",
    invalid: bool = False,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "answer": "模型原正文\n\n模型额外断言",
        "claims": [
            {
                "text": f"有据{i}" if i <= cited else f"未支撑{i}",
                "refs": [1] if i <= cited else ([99] if invalid else []),
            }
            for i in range(1, total + 1)
        ],
    }


def _generated_capability(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    empty_pack: bool = False,
) -> tuple[RagQueryCapability, _RawJsonClient, list[int]]:
    client = _RawJsonClient(payload)
    seen_k: list[int] = []

    def fake_retrieve(
        store: Any, question: str, k: int = 5, **kwargs: Any
    ) -> EvidencePack:
        seen_k.append(k)
        return _pack(0 if empty_pack else 2)

    def forbid_default_client() -> Any:
        raise AssertionError("该回归必须注入假客户端，不能读取凭证或调用真实 API")

    monkeypatch.setattr(rag_cap, "retrieve", fake_retrieve)
    monkeypatch.setattr(RagQueryCapability, "_get_store", lambda self: object())
    monkeypatch.setattr(rag_answer, "get_client", forbid_default_client)
    assert rag_cap.generate is rag_answer.generate
    return RagQueryCapability(client=client), client, seen_k


@pytest.mark.parametrize(
    ("total", "cited", "invalid", "expected_outcome", "expected_status", "coverage"),
    [
        (10, 1, False, "partial", ResultStatus.success, 0.1),
        (10, 0, False, "insufficient", ResultStatus.weak, 0.0),
        (10, 0, True, "insufficient", ResultStatus.weak, 0.0),
        (10, 10, False, "answered", ResultStatus.success, 1.0),
        (2, 1, False, "partial", ResultStatus.success, 0.5),
        (0, 0, False, "insufficient", ResultStatus.weak, 0.0),
    ],
)
async def test_generated_adapter_preserves_original_denominator(
    monkeypatch, total, cited, invalid, expected_outcome, expected_status, coverage
):
    cap, client, seen_k = _generated_capability(
        monkeypatch, _raw_claims_payload(total, cited, invalid=invalid)
    )
    result = await cap.run(_ctx())
    assert client.calls == result.llm_calls == 1
    assert seen_k == [5]
    assert result.status == expected_status
    assert result.output["outcome"] == expected_outcome
    assert result.confidence_signals["grounding_coverage"] == coverage
    assert "模型" not in result.output["answer_markdown"]
    assert "未支撑" not in result.output["answer_markdown"]
    assert result.evidence_refs == (["doc1.pdf#p1"] if cited else [])
    if expected_outcome == "answered":
        assert result.output["citations"] == [
            {"anchor": 1, "doc_id": "doc1.pdf", "pages": [1, 1], "section": "1 总则"}
        ]
    else:
        assert "citations" not in result.output
    if expected_outcome == "partial":
        assert result.output["answer_markdown"].startswith("证据不完整：")
    if expected_status == ResultStatus.weak:
        assert result.code == "insufficient_evidence"


async def test_generated_adapter_keeps_conservative_width(monkeypatch):
    cap, client, seen_k = _generated_capability(
        monkeypatch, _raw_claims_payload(2, 1, outcome="partial")
    )
    result = await cap.run(_ctx({"conservative": True, "topk_factor": 1.5}))
    assert seen_k == [8]
    assert client.calls == result.llm_calls == 1
    assert result.confidence_signals["grounding_coverage"] == 0.5
    assert result.output["answer_markdown"] == (
        "证据不完整：以下仅列出有有效引文的声明，其余内容无法确认。 有据1 [1]"
    )


async def test_generated_adapter_empty_pack_costs_no_llm_call(monkeypatch):
    cap, client, seen_k = _generated_capability(
        monkeypatch, _raw_claims_payload(10, 10), empty_pack=True
    )
    result = await cap.run(_ctx())
    assert seen_k == [5]
    assert client.calls == result.llm_calls == 0
    assert result.status == ResultStatus.weak
    assert result.code == "insufficient_evidence"
    assert result.confidence_signals["grounding_coverage"] == 0.0
    assert "模型" not in result.output["answer_markdown"]


def _generated_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    with_alternate: bool = False,
) -> tuple[Orchestrator, _RawJsonClient]:
    cap, client, _ = _generated_capability(monkeypatch, payload)
    entry = CapabilityCatalogEntry(
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
        fallbacks=(
            ["alternate", "human_handoff"] if with_alternate else ["human_handoff"]
        ),
        validation_rules=["grounding_coverage_min"],
    )
    registry = Registry(entries={entry.name: entry}, impls={entry.name: cap})
    if with_alternate:
        alternate = CapabilityCatalogEntry(
            name="alternate",
            owner="platform",
            domain_scope="records",
            capability_version="0.1.0",
            rollout_status="active",
            task_types_supported=["alternate_only"],
            use_when=["records"],
            avoid_when=["nothing"],
            tool_schema_bundle=["alternate.read"],
            output_contract=OutputContract(required_fields=["answer_markdown"]),
            fallbacks=["human_handoff"],
            validation_rules=[],
        )
        registry.entries[alternate.name] = alternate
        registry.impls[alternate.name] = ScriptedCapability(
            [
                {
                    "status": "success",
                    "output": {"answer_markdown": "替代能力的独立结果"},
                    "tool_steps": ["alternate.read"],
                }
            ]
        )
    strong = {
        "task_type": "faq_howto",
        "top_match_score": 0.9,
        "candidate_count": 1,
        "model": {"confidence": 0.9},
    }
    store = Store(tmp_path / "generated-rag.db")
    return Orchestrator(store, registry, FakeFrontHalf([strong])), client


@pytest.mark.parametrize("with_alternate", [False, True])
def test_generated_runtime_one_of_ten_hits_v3(tmp_path, monkeypatch, with_alternate):
    orch, client = _generated_orchestrator(
        tmp_path,
        monkeypatch,
        _raw_claims_payload(10, 1),
        with_alternate=with_alternate,
    )
    try:
        turn = orch.run_turn("年假怎么休")
        assert client.calls == 1
        objects = orch.store.objects(turn.request_id)
        executions = [o["payload"] for o in objects if o["kind"] == "execution"]
        first = executions[0]["result"]
        assert first["confidence_signals"]["grounding_coverage"] == 0.1
        assert first["output"]["outcome"] == "partial"
        assert first["output"]["answer_markdown"] == (
            "证据不完整：以下仅列出有有效引文的声明，其余内容无法确认。 有据1 [1]"
        )
        assert first["evidence_refs"] == ["doc1.pdf#p1"]
        assert "citations" not in first["output"]
        events = orch.store.events(turn.request_id)
        validations = [
            e["payload"] for e in events if e["event"] == "validation_completed"
        ]
        assert validations[0]["execution_row_id"] == "exec_e6_proceed_grounded"
        assert validations[0]["validation_row_id"] == "val_v3_switch_or_retry_grounding"
        assert validations[0]["action"] == "switch_capability"
        assert "未支撑" not in turn.response
        assert "模型" not in turn.response
        assert "有据1" not in turn.response
        if with_alternate:
            assert [e["capability_name"] for e in executions] == [
                "rag_query",
                "alternate",
            ]
            assert turn.status.value == "completed"
            assert turn.outcome_type.value == "answered"
            assert turn.response == "替代能力的独立结果"
            assert validations[-1]["validation_row_id"] == "val_v5_accept"
        else:
            assert len(executions) == 1
            assert turn.status.value == "handoff"
            assert turn.outcome_type.value == "human_handoff"
            assert not any(o["kind"] == "outcome" for o in objects)
    finally:
        orch.store.close()


@pytest.mark.parametrize(
    ("payload", "expected_outcome", "coverage", "validation_row"),
    [
        (_raw_claims_payload(2, 1), "partial_answer", 0.5, "val_v6_partial_answer"),
        (
            _raw_claims_payload(2, 2, outcome="partial"),
            "partial_answer",
            1.0,
            "val_v6_partial_answer",
        ),
        (_raw_claims_payload(10, 10), "answered", 1.0, "val_v5_accept"),
    ],
)
def test_generated_runtime_safe_completion(
    tmp_path, monkeypatch, payload, expected_outcome, coverage, validation_row
):
    orch, client = _generated_orchestrator(tmp_path, monkeypatch, payload)
    try:
        turn = orch.run_turn("年假怎么休")
        assert client.calls == 1
        assert turn.status.value == "completed"
        assert turn.outcome_type.value == expected_outcome
        assert "模型" not in turn.response
        assert "未支撑" not in turn.response
        assert "有据1 [1]" in turn.response
        if expected_outcome == "partial_answer":
            assert turn.response.startswith("证据不完整：")
        objects = orch.store.objects(turn.request_id)
        execution = next(o["payload"] for o in objects if o["kind"] == "execution")
        assert (
            execution["result"]["confidence_signals"]["grounding_coverage"] == coverage
        )
        final = next(o["payload"] for o in objects if o["kind"] == "outcome")
        assert final["user_response"] == turn.response
        assert final["validation_summary"]["grounding"] == "pass"
        events = orch.store.events(turn.request_id)
        validation = next(
            e["payload"] for e in events if e["event"] == "validation_completed"
        )
        assert validation["validation_row_id"] == validation_row
    finally:
        orch.store.close()


@pytest.mark.parametrize(
    "payload",
    [
        _raw_claims_payload(10, 0),
        _raw_claims_payload(10, 0, invalid=True),
        _raw_claims_payload(0, 0),
        _raw_claims_payload(1, 0, outcome="insufficient"),
        _raw_claims_payload(1, 0, outcome="escalated"),
        _raw_claims_payload(1, 0, outcome="refuse"),
    ],
)
def test_generated_runtime_refusal_is_not_completed(tmp_path, monkeypatch, payload):
    orch, client = _generated_orchestrator(tmp_path, monkeypatch, payload)
    try:
        turn = orch.run_turn("年假怎么休")
        assert client.calls == 1
        assert turn.status.value == "handoff"
        assert turn.outcome_type.value == "human_handoff"
        assert "未支撑" not in turn.response
        assert "模型" not in turn.response
        objects = orch.store.objects(turn.request_id)
        execution = next(o["payload"] for o in objects if o["kind"] == "execution")
        assert execution["result"]["status"] == "weak"
        assert execution["result"]["confidence_signals"]["grounding_coverage"] == 0.0
        assert execution["result"]["output"]["answer_markdown"] == (
            "证据不足，无法提供有有效引文支持的回答。"
        )
        validations = [
            e["payload"]
            for e in orch.store.events(turn.request_id)
            if e["event"] == "validation_completed"
        ]
        assert validations[0]["execution_row_id"] == "exec_e7_handoff_default"
        assert validations[0]["validation_row_id"] == ""
    finally:
        orch.store.close()


@pytest.mark.parametrize("in_field", [True, False])
def test_generated_runtime_clarify_survives_alternate(tmp_path, monkeypatch, in_field):
    question = "你想问哪类假？"
    payload = {
        "outcome": "clarify",
        "answer": "" if in_field else question,
        "claims": [],
        "clarification": question if in_field else "",
    }
    orch, client = _generated_orchestrator(
        tmp_path, monkeypatch, payload, with_alternate=True
    )
    try:
        turn = orch.run_turn("假怎么休")
        assert client.calls == 1
        assert turn.status.value == "awaiting_clarification"
        assert turn.outcome_type.value == "clarification_requested"
        assert turn.question == question
        objects = orch.store.objects(turn.request_id)
        assert len([o for o in objects if o["kind"] == "execution"]) == 1
        validations = [
            e["payload"]
            for e in orch.store.events(turn.request_id)
            if e["event"] == "validation_completed"
        ]
        assert validations[0]["execution_row_id"] == "exec_e5_clarify_user_constraint"
        assert validations[0]["validation_row_id"] == ""
    finally:
        orch.store.close()
