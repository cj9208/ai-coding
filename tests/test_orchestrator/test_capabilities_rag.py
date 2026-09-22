"""M2 rag adapter: outcome mapping, DP-10 conservative widening, grounding
coverage — all against monkeypatched rag functions (zero LLM, zero corpus).
"""

from pathlib import Path
from typing import Any

import pytest

from orchestrator.capabilities import rag as rag_cap
from orchestrator.capabilities.rag import RagQueryCapability
from orchestrator.contracts import CapabilityContext, ResultStatus
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
