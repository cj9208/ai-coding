"""Adapter over the shipped rag query path (design DP-6, DP-10).

The adapter is deliberately dumb — the whole point of the architecture:
- it calls ``rag.engine.retrieve`` + ``rag.answer.generate`` and translates
  the rag ``Answer.outcome`` into the structured ``CapabilityResult``
  vocabulary; it never decides retry/escalation/acceptance itself;
- DP-10: the routing decision's conservative constraint is honored here and
  only here — ``topk_factor`` widens retrieval (ceil), nothing downstream
  re-widens it;
- grounding: rag reports fully-cited answers via its own integrity check;
  the adapter turns that into ``grounding_coverage`` (cited claims / claims),
  and withholds ``citations`` when the answer is only partial — the
  validation table then speaks (v3 switch on low coverage, v6
  partial_answer on useful-but-incomplete).
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

from rag.answer import Outcome, generate
from rag.contract import Answer, EvidencePack
from rag.engine import retrieve
from rag.store import RagStore
from utils.paths import data_dir

from ..contracts import CapabilityContext, CapabilityResult, ResultStatus

DEFAULT_K = 5


class RagQueryCapability:
    """Capability-protocol implementation: one run = one retrieve+generate."""

    def __init__(
        self,
        *,
        kb_path: Path | None = None,
        k: int = DEFAULT_K,
        client: Any = None,
    ) -> None:
        self._kb_path = kb_path or data_dir("rag") / "kb.db"
        self._k = k
        self._client = client
        self._store: Any = None

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        k = self._k
        if ctx.constraints.get("conservative"):
            k = math.ceil(k * float(ctx.constraints.get("topk_factor", 1.5)))
        try:
            pack = retrieve(self._get_store(), ctx.normalized_query, k=k)
            answer = asyncio.run(
                generate(pack, ctx.normalized_query, client=self._client)
            )
        except Exception as exc:  # a dead corpus is a structured failure
            return CapabilityResult(
                status=ResultStatus.failed,
                code="dependency_unavailable",
                output={"error": str(exc)[:200]},
            )
        return self._translate(answer, pack)

    # -- internals ---------------------------------------------------------
    def _get_store(self) -> Any:
        if self._store is None:
            self._store = RagStore(self._kb_path)
        return self._store

    def _translate(self, answer: Answer, pack: EvidencePack) -> CapabilityResult:
        coverage = _coverage(answer)
        signals = {"grounding_coverage": coverage}
        tool_steps = [
            f"rag.retrieve(k={len(pack.chunks)})",
            f"rag.{answer.outcome.value}",
        ]
        evidence = _evidence_refs(answer, pack)

        if answer.outcome in (Outcome.clarify,):
            return CapabilityResult(
                status=ResultStatus.weak,
                code="user_constraint_missing",
                output={
                    "clarification": answer.clarification or answer.text,
                    "outcome": answer.outcome.value,
                },
                confidence_signals=signals,
                tool_steps=tool_steps,
            )
        if answer.outcome in (Outcome.insufficient, Outcome.escalated):
            return CapabilityResult(
                status=ResultStatus.weak,
                code="insufficient_evidence",
                output={
                    "answer_markdown": answer.text,
                    "outcome": answer.outcome.value,
                },
                evidence_refs=evidence,
                confidence_signals=signals,
                tool_steps=tool_steps,
            )
        output: dict[str, Any] = {
            "answer_markdown": answer.text,
            "outcome": answer.outcome.value,
        }
        if answer.outcome == Outcome.answered:
            output["citations"] = _citations(answer, pack)
        if answer.notes:
            output["evidence_notes"] = answer.notes
        return CapabilityResult(
            status=ResultStatus.success,
            output=output,
            evidence_refs=evidence,
            confidence_signals=signals,
            tool_steps=tool_steps,
        )


def _coverage(answer: Answer) -> float:
    claims = [c for c in answer.claims if c.text.strip()]
    if not claims:
        return 0.0
    cited = len([c for c in claims if c.refs])
    return round(cited / len(claims), 3)


def _referenced(answer: Answer, pack: EvidencePack) -> set[int]:
    return {r for c in answer.claims for r in c.refs if 1 <= r <= len(pack.chunks)}


def _citations(answer: Answer, pack: EvidencePack) -> list[dict[str, Any]]:
    refs = _referenced(answer, pack)
    return [
        {
            "anchor": i + 1,
            "doc_id": chunk.doc_id,
            "pages": [chunk.page_span[0] + 1, chunk.page_span[1] + 1],
            "section": chunk.section_path,
        }
        for i, chunk in enumerate(pack.chunks)
        if i + 1 in refs
    ]


def _evidence_refs(answer: Answer, pack: EvidencePack) -> list[str]:
    refs = _referenced(answer, pack)
    return [
        f"{chunk.doc_id}#p{chunk.page_span[0] + 1}"
        for i, chunk in enumerate(pack.chunks)
        if i + 1 in refs
    ]
