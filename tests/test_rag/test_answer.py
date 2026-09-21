"""Grounded answering: integrity gate + LLM wiring with a fake client."""

from __future__ import annotations

import asyncio

import pytest

from rag.answer import build_prompt, generate, validate_answer
from rag.contract import Answer, Chunk, ChunkType, Claim, EvidencePack, Outcome
from rag.enrich import LlmEnricher


def _chunk(i: int) -> Chunk:
    return Chunk(
        chunk_id=f"c{i}",
        doc_id="d1",
        chunk_type=ChunkType.prose,
        is_parent=False,
        parent_chunk_id=None,
        section_path="1 年假",
        text=f"片段{i}：年假需要审批。",
        page_span=(0, 0),
        block_keys=[f"0:{i}"],
        content_hash="h",
    )


@pytest.fixture
def pack() -> EvidencePack:
    return EvidencePack(query="q", chunks=[_chunk(1), _chunk(2)])


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def chat_json(
        self, prompt, system_prompt="", *, schema=None, model=None, temperature=None
    ):
        self.calls += 1
        if schema is not None:
            return schema.model_validate(self.payload)
        return self.payload


def test_claims_with_dead_refs_downgrade_to_partial(pack):
    raw = Answer(
        outcome=Outcome.answered,
        text="ans",
        claims=[Claim(text="a", refs=[1]), Claim(text="b", refs=[99])],
    )
    out = validate_answer(raw, pack)
    assert out.outcome == Outcome.partial
    assert len(out.claims) == 1


def test_uncited_claim_downgrades(pack):
    raw = Answer(
        outcome=Outcome.answered,
        text="ans",
        claims=[Claim(text="a", refs=[1]), Claim(text="inferred", refs=[])],
    )
    out = validate_answer(raw, pack)
    assert out.outcome == Outcome.partial


def test_unknown_outcome_becomes_insufficient(pack):
    raw = Answer(outcome=Outcome.insufficient, text="x")  # enum-pinned already
    assert validate_answer(raw, pack).outcome == Outcome.insufficient


def test_clarify_and_insufficient_pass_through(pack):
    for outcome in (Outcome.clarify, Outcome.escalated):
        out = validate_answer(Answer(outcome=outcome, clarification="?"), pack)
        assert out.outcome == outcome


def test_prompt_lists_anchors_and_context(pack):
    pack.parents = [_chunk(9).model_copy(update={"is_parent": True})]
    prompt = build_prompt(pack, "年假几天？")
    assert "[1]" in prompt and "[2]" in prompt
    assert "[context-1]" in prompt
    assert "cite only numbered evidence" in prompt


def test_generate_short_circuits_without_llm_when_insufficient():
    client = FakeClient({"outcome": "answered", "answer": "x", "claims": []})
    empty = EvidencePack(query="q", insufficient=True, notes=["none"])
    answer = asyncio.run(generate(empty, "q", client=client))
    assert answer.outcome == Outcome.insufficient
    assert client.calls == 0


def test_generate_happy_path_with_fake_llm(pack):
    client = FakeClient(
        {
            "outcome": "answered",
            "answer": "五天，超过三天需经理审批。",
            "claims": [{"text": "入职满一年享五天年假", "refs": [1]}],
        }
    )
    answer = asyncio.run(generate(pack, "年假几天？", client=client))
    assert answer.outcome == Outcome.answered
    assert answer.claims[0].refs == [1]


def test_enricher_writes_only_inferred_fields():
    chunk = _chunk(1)
    client = FakeClient({"title": "t", "keywords": ["年假"], "summary": "s"})
    from rag.contract import Chunk as C  # schema path exercised via kwargs

    enricher = LlmEnricher(client=client)  # type: ignore[arg-type]
    asyncio.run(enricher.enrich([chunk]))
    assert chunk.inferred["keywords"] == ["年假"]
    assert chunk.text == "片段1：年假需要审批。"  # source truth untouched
    assert C.model_validate(chunk.model_dump())
