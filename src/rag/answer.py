"""基于 evidence 的生成与确定性引用校验。

保留全部非空声明及清洗后的 refs，供下游从同一列表计算覆盖率。
事实回答只由可引用声明重建，不复用生成器的独立 answer 正文。
证据包不足时仍在模型调用前短路；引用完整性不等于语义蕴含验证。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from llm_client import LLMClient, get_client

from .contract import Answer, Claim, EvidencePack, Outcome

_MAX_CHUNK_CHARS = 1500

# 有意泛化：数字、context 及混合列表（如 [1,context-2]）一律剥离，
# 展示锚点只从 refs 生成；[note]、[1st] 等非引用括号保留。
_INLINE_CITATION = re.compile(
    r"\[\s*(?:[+-]?\d+|context-\d+)(?:\s*[,，;；\-–]\s*(?:[+-]?\d+|context-\d+))*\s*\]"
)
_PARTIAL_NOTICE = "证据不完整：以下仅列出有有效引文的声明，其余内容无法确认。"
_REFUSAL_TEXT = "证据不足，无法提供有有效引文支持的回答。"


def _claim_body(text: str) -> str:
    """移除内联引用并保留各行内容；展示锚点只从 refs 生成。"""
    return " ".join(_INLINE_CITATION.sub("", text).split())


_SYSTEM = (
    "You are a grounded answering module. Answer ONLY from the numbered "
    "evidence below. Every factual claim must cite its source passages as "
    "[n] (1-based). If the evidence does not support an answer, choose "
    "outcome 'insufficient' — do not guess. Answer in the question's "
    "language. Distinguish direct evidence from labelled inference."
)

_SCHEMA_HINT = (
    'JSON: {"outcome": answered|partial|clarify|insufficient|escalated, '
    '"answer": str, "claims": [{"text": str, "refs": [int]}], '
    '"clarification": str}'
)


class _RawAnswer(BaseModel):
    outcome: str = "insufficient"
    answer: str = ""
    claims: list[Claim] = Field(default_factory=list)
    clarification: str = ""


def build_prompt(pack: EvidencePack, question: str) -> str:
    lines = [f"Question: {question}", "", "Evidence:"]
    for i, chunk in enumerate(pack.chunks, start=1):
        text = chunk.text[:_MAX_CHUNK_CHARS]
        span = f"p{chunk.page_span[0] + 1}"
        if chunk.page_span[1] != chunk.page_span[0]:
            span += f"-{chunk.page_span[1] + 1}"
        lines.append(
            f"[{i}] ({chunk.doc_id} {span}"
            f"; {chunk.chunk_type}; section: {chunk.section_path or '/'})\n{text}"
        )
    for j, parent in enumerate(pack.parents, start=1):
        lines.append(
            f"[context-{j}] section: {parent.section_path}\n"
            f"{parent.text[: _MAX_CHUNK_CHARS * 2]}"
        )
    lines += [
        "",
        "Context blocks are background; cite only numbered evidence.",
        _SCHEMA_HINT,
    ]
    return "\n".join(lines)


def validate_answer(answer: Answer, pack: EvidencePack) -> Answer:
    """保留声明分母，同时构造独立于模型原正文的安全展示文本。"""
    if answer.outcome not in set(Outcome):
        return Answer(
            outcome=Outcome.insufficient,
            text=_REFUSAL_TEXT,
            notes=f"unknown outcome from generator: {answer.outcome!r}",
        )

    n = len(pack.chunks)
    notes = [answer.notes] if answer.notes else []
    checked: list[Claim] = []
    rendered: list[str] = []
    refs_lost = 0
    for claim in answer.claims:
        if not claim.text.strip():
            continue
        refs = [r for r in claim.refs if 1 <= r <= n]
        if len(refs) != len(claim.refs):
            refs_lost += 1
        body = _claim_body(claim.text)
        if not body:
            refs = []
        checked.append(claim.model_copy(update={"refs": refs}))
        if refs:
            anchors = " ".join(f"[{r}]" for r in dict.fromkeys(refs))
            rendered.append(f"{body} {anchors}")

    uncited = sum(not claim.refs for claim in checked)
    if refs_lost:
        notes.append(f"{refs_lost} claim(s) lost refs that point outside evidence")
    if uncited:
        notes.append(f"{uncited} claim(s) have no valid citation")

    outcome = answer.outcome
    clarification = answer.clarification
    if outcome == Outcome.clarify:
        text = ""
        clarification = clarification or answer.text
    elif outcome in (Outcome.insufficient, Outcome.escalated):
        text = _REFUSAL_TEXT
    elif not rendered:
        outcome = Outcome.insufficient
        text = _REFUSAL_TEXT
        notes.append("no citable claim remains for display")
    else:
        if outcome == Outcome.answered and (refs_lost or uncited):
            outcome = Outcome.partial
            notes.append("downgraded: not every claim traces to a valid citation")
        text = " ".join(rendered)
        if outcome == Outcome.partial:
            text = f"{_PARTIAL_NOTICE} {text}"

    return Answer(
        outcome=outcome,
        text=text,
        claims=checked,
        clarification=clarification,
        notes="; ".join(notes),
    )


def _to_answer(raw: Any) -> Answer:
    if isinstance(raw, ValidationError):
        return Answer(outcome=Outcome.insufficient, notes=[str(raw)])
    if isinstance(raw, dict):
        raw = _RawAnswer.model_validate(raw)
    try:
        outcome = Outcome(raw.outcome)
    except ValueError:
        outcome = Outcome.insufficient
    return Answer(
        outcome=outcome,
        text=raw.answer,
        claims=raw.claims,
        clarification=raw.clarification,
    )


async def generate(
    pack: EvidencePack, question: str, client: LLMClient | None = None
) -> Answer:
    """The full answering step: gate first, then LLM, then integrity."""
    if pack.insufficient or not pack.chunks:
        return Answer(
            outcome=Outcome.insufficient,
            notes="; ".join(pack.notes) or "evidence too weak",
        )
    client = client or get_client()
    raw = await client.chat_json(
        build_prompt(pack, question), system_prompt=_SYSTEM, temperature=0.2
    )
    return validate_answer(_to_answer(raw), pack)
