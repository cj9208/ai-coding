"""Thin grounded generation + deterministic citation integrity (CH03_04).

The generator sees only the evidence pack, must cite with [n] anchors, and
picks one of the five outcomes; everything after it is plain code:
``validate_answer`` strips claims whose refs point nowhere and downgrades
overconfident outcomes. Insufficient evidence short-circuits **before** the
LLM call — abstention is cheap, and the model is never asked to
compensate for missing retrieval.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from llm_client import LLMClient, get_client

from .contract import Answer, Claim, EvidencePack, Outcome

_MAX_CHUNK_CHARS = 1500

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
    """Pure, deterministic post-generation gate."""
    n = len(pack.chunks)
    notes: list[str] = []
    if answer.outcome not in set(Outcome):
        return Answer(
            outcome=Outcome.insufficient,
            notes=[f"unknown outcome from generator: {answer.outcome!r}"],
        )
    checked: list[Claim] = []
    dropped = 0
    uncited = 0
    for claim in answer.claims:
        refs = [r for r in claim.refs if 1 <= r <= n]
        if refs:
            if len(refs) != len(claim.refs):
                dropped += 1
            checked.append(claim.model_copy(update={"refs": refs}))
        elif claim.text.strip():
            uncited += 1
    if dropped:
        notes.append(f"{dropped} claim(s) lost refs that point outside evidence")
    if uncited:
        notes.append(f"{uncited} claim(s) carried no citation at all")

    outcome = answer.outcome
    if outcome == Outcome.answered:
        if n == 0 or not checked or dropped or uncited:
            outcome = Outcome.partial
            notes.append("downgraded: not every claim traces to a valid citation")
    return Answer(
        outcome=outcome,
        text=answer.text,
        claims=checked,
        clarification=answer.clarification,
        notes="; ".join(notes) if notes else answer.notes,
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
