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
    assert [c.text for c in out.claims] == ["a", "b"]
    assert [c.refs for c in out.claims] == [[1], []]
    assert out.text == (
        "证据不完整：以下仅列出有有效引文的声明，其余内容无法确认。 a [1]"
    )
    assert raw.claims[1].refs == [99]
    assert raw.text == "ans"


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


@pytest.mark.parametrize(
    ("total", "cited", "raw_outcome", "expected_outcome"),
    [
        (10, 1, "answered", Outcome.partial),
        (10, 0, "answered", Outcome.insufficient),
        (10, 10, "answered", Outcome.answered),
        (2, 1, "partial", Outcome.partial),
        (2, 2, "partial", Outcome.partial),
        (0, 0, "answered", Outcome.insufficient),
        (0, 0, "partial", Outcome.insufficient),
    ],
)
def test_generate_preserves_denominator_and_rebuilds_text(
    pack, total, cited, raw_outcome, expected_outcome
):
    claims = [
        {
            "text": f"有据{i}" if i <= cited else f"未支撑{i}",
            "refs": [1] if i <= cited else [],
        }
        for i in range(1, total + 1)
    ]
    payload = {
        "outcome": raw_outcome,
        "answer": "模型原正文第一段\n\n模型额外断言第二段",
        "claims": claims,
    }
    client = FakeClient(payload)
    result = asyncio.run(generate(pack, "年假怎么休", client=client))
    assert client.calls == 1
    assert result.outcome == expected_outcome
    assert len(result.claims) == total
    assert [c.text for c in result.claims] == [c["text"] for c in claims]
    assert [c.refs for c in result.claims] == [c["refs"] for c in claims]
    assert "模型原正文" not in result.text
    assert "模型额外断言" not in result.text
    assert "未支撑" not in result.text
    if cited:
        expected = " ".join(f"有据{i} [1]" for i in range(1, cited + 1))
        if expected_outcome == Outcome.partial:
            expected = (
                "证据不完整：以下仅列出有有效引文的声明，其余内容无法确认。 " + expected
            )
        assert result.text == expected
    else:
        assert result.text == "证据不足，无法提供有有效引文支持的回答。"


def test_generate_cleans_refs_without_mutating_claim_text(pack):
    payload = {
        "outcome": "answered",
        "answer": "不可照搬的多行正文\n第二行也不可照搬",
        "claims": [
            {"text": "年假[99]\n需要审批[context-1]", "refs": [0, 1, 99, -1]},
            {"text": "未支撑[99]", "refs": [-1, 0, 99]},
            {"text": "   \n ", "refs": [1]},
        ],
    }
    result = asyncio.run(generate(pack, "年假怎么休", client=FakeClient(payload)))
    assert result.outcome == Outcome.partial
    assert [c.text for c in result.claims] == [
        "年假[99]\n需要审批[context-1]",
        "未支撑[99]",
    ]
    assert [c.refs for c in result.claims] == [[1], []]
    assert result.text == (
        "证据不完整：以下仅列出有有效引文的声明，其余内容无法确认。 "
        "年假 需要审批 [1]"
    )
    assert payload["claims"][0]["refs"] == [0, 1, 99, -1]


@pytest.mark.parametrize(
    "marker", ["[99]", "[0]", "[-1]", "[+99]", "[1,99]", "[1-99]", "[context-1]"]
)
def test_generate_renders_only_structured_refs(pack, marker):
    client = FakeClient(
        {
            "outcome": "answered",
            "answer": "另一份正文中的非法引用[99]",
            "claims": [{"text": f"年假{marker}需要审批", "refs": [1]}],
        }
    )
    result = asyncio.run(generate(pack, "q", client=client))
    assert result.outcome == Outcome.answered
    assert result.claims[0].text == f"年假{marker}需要审批"
    assert result.claims[0].refs == [1]
    assert result.text == "年假需要审批 [1]"


@pytest.mark.parametrize(
    "claims",
    [
        [{"text": "未支撑", "refs": [99, 0, -1]}],
        [{"text": " \n ", "refs": [1]}],
        [{"text": "[1]", "refs": [1]}],
    ],
)
def test_generate_refuses_when_no_claim_can_be_displayed(pack, claims):
    client = FakeClient(
        {"outcome": "answered", "answer": "不能展示的结论", "claims": claims}
    )
    result = asyncio.run(generate(pack, "q", client=client))
    assert result.outcome == Outcome.insufficient
    assert result.text == "证据不足，无法提供有有效引文支持的回答。"
    assert all(not c.refs for c in result.claims)
    assert len(result.claims) == sum(bool(c["text"].strip()) for c in claims)


@pytest.mark.parametrize("raw_outcome", ["insufficient", "escalated", "refuse"])
def test_generate_preserves_refusal_semantics_without_raw_body(pack, raw_outcome):
    client = FakeClient(
        {
            "outcome": raw_outcome,
            "answer": "拒答时夹带的未支撑结论",
            "claims": [{"text": "未支撑", "refs": []}],
        }
    )
    result = asyncio.run(generate(pack, "q", client=client))
    expected = Outcome.escalated if raw_outcome == "escalated" else Outcome.insufficient
    assert result.outcome == expected
    assert result.text == "证据不足，无法提供有有效引文支持的回答。"
    assert len(result.claims) == 1
    assert result.claims[0].refs == []
    assert client.calls == 1


@pytest.mark.parametrize("in_field", [True, False])
def test_generate_preserves_clarification_without_claims(pack, in_field):
    question = "你想问哪类假？"
    client = FakeClient(
        {
            "outcome": "clarify",
            "answer": "" if in_field else question,
            "claims": [],
            "clarification": question if in_field else "",
        }
    )
    result = asyncio.run(generate(pack, "q", client=client))
    assert result.outcome == Outcome.clarify
    assert result.clarification == question
    assert result.text == ""
    assert result.claims == []


def test_generate_keeps_all_cited_multiline_claim_content(pack):
    client = FakeClient(
        {
            "outcome": "answered",
            "answer": "模型额外断言\n\n另一个额外断言",
            "claims": [
                {"text": "入职满一年\n享五天年假[99]", "refs": [1]},
                {"text": "超过三天\n需要经理审批[1]", "refs": [2]},
            ],
        }
    )
    result = asyncio.run(generate(pack, "q", client=client))
    assert result.outcome == Outcome.answered
    assert result.text == "入职满一年 享五天年假 [1] 超过三天 需要经理审批 [2]"
    assert [c.refs for c in result.claims] == [[1], [2]]


def test_validate_answer_preserves_existing_notes(pack):
    result = validate_answer(
        Answer(
            outcome=Outcome.answered,
            text="不可直接使用的正文",
            claims=[Claim(text="有据", refs=[1]), Claim(text="未支撑", refs=[])],
            notes="原有检索说明",
        ),
        pack,
    )
    assert "原有检索说明" in result.notes
    assert "no valid citation" in result.notes
