"""The real front half: safety gate -> normalize -> flash-model
interpretation over ``llm_client`` (module map: "strict-JSON schema =
contracts").

Seam discipline:
- this class satisfies the same ``FrontHalf`` protocol as M0's
  ``FakeFrontHalf`` — ``runtime.py`` does not know or care which one it got;
- the LLM **proposes** (task type, attributes, ambiguity, a clarifying
  question), the harness **decides** (safety verdict, deterministic
  signals, and every budget/routing consequence downstream);
- ``refuse``/``handoff`` from the safety gate short-circuit before any call
  — a refused request never costs a token;
- the three language-scoped front-half assets (safety table, alias table,
  prompt) are selected by ``envelope.original_input.locale`` — caller
  context, never model output — and a missing pack clarifies instead of
  allowing (05b step 1, ``orchestrator.packs``);
- one ``interpret`` call = at most one model call, **awaited** — the
  async ``llm_client`` method is called directly (05a step 5: no
  ``asyncio.run`` inside the turn, which is what lets the harness be
  embedded in an ASGI host).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import Field, field_validator

from . import safety as safety_gate
from .config import Models
from .contracts import (
    DeterministicSignals,
    FrontHalfOutput,
    InterpretationRecord,
    ModelSignals,
    RequestEnvelope,
    SafetyDecision,
    _Contract,
)
from .ids import new_id
from .normalize import normalize
from .packs import LocalePack, get_pack


#: the strict-JSON shape the flash model must return — everything the model
#: may propose, and nothing else. Unknown fields are ignored, so the harness
#: can never be manipulated into a field it does not model.
class ModelInterpretation(_Contract):
    task_type: str = "other"
    target_entity_guess: str | None = None
    requested_attributes: list[str] = Field(default_factory=list)
    interpretation_summary: str = ""
    confidence: float = 0.0
    ambiguity_flags: list[str] = Field(default_factory=list)
    alternative_interpretations: list[str] = Field(default_factory=list)
    clarification_question: str = ""
    user_resolvable_ambiguity: bool = False
    missing_required_constraint: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "user_resolvable_ambiguity", "missing_required_constraint", mode="before"
    )
    @classmethod
    def _coerce_bool(cls, v: Any) -> Any:
        """Flash models often write *what is missing* into these boolean
        fields; this is a proposal boundary, so coerce instead of bouncing
        a repair round-trip (seconds of the wall-clock budget). A
        description means the flag is set; only clear negations read False."""
        if not isinstance(v, str):
            return v
        s = v.strip().lower()
        if s in ("false", "0", "no", "n", "否", "无", "", "none", "null"):
            return False
        return True


#: the task-type vocabulary is harness-wide (the routing table and the
#: registry both key on it), so only its *prose* is localized — the
#: enumeration is injected into each pack's instruction template.
_TASK_TYPES = "faq_howto | lookup | comparison | process_question | other"


class LlmFrontHalf:
    """``FrontHalf`` implementation for production use (and for zero-network
    tests when given a stub ``client`` — see tests/test_orchestrator)."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from llm_client import get_client

            self._client = get_client()
        return self._client

    async def interpret(
        self,
        envelope: RequestEnvelope,
        *,
        answer: str | None = None,
        escalated: bool = False,
    ) -> FrontHalfOutput:
        text = envelope.original_input.text
        # locale is caller context (set by run_turn), never model output —
        # a proposal must not choose the table that gates it (05b)
        pack = get_pack(envelope.original_input.locale)
        if pack is None:
            # unchecked input: clarify without spending a token on an
            # interpretation the harness would not route anyway
            return self._assemble(
                envelope,
                normalize(text, aliases={}),
                safety_gate.evaluate(text, envelope.original_input.locale),
                answer=answer,
                model=None,
                model_name="none",
            )
        verdict = safety_gate.evaluate(text, pack.locale)
        if verdict.decision in (SafetyDecision.refuse, SafetyDecision.handoff):
            # hard stop: deterministic, before any probabilistic step
            norm = normalize(text, aliases=pack.aliases)
            return self._assemble(
                envelope, norm, verdict, answer=answer, model=None, model_name="none"
            )

        norm = normalize(f"{text} {answer}" if answer else text, aliases=pack.aliases)
        model_name = Models.ESCALATED if escalated else Models.FLASH
        raw = await self.client.chat_json(
            self._prompt(pack, envelope, norm, answer, escalated),
            pack.prompt.system,
            schema=ModelInterpretation,
            **({"model": model_name} if model_name else {}),
        )
        interp = (
            raw
            if isinstance(raw, ModelInterpretation)
            else ModelInterpretation.model_validate(raw)
        )
        return self._assemble(
            envelope,
            norm,
            verdict,
            answer=answer,
            model=interp,
            model_name=model_name or "default",
        )

    # -- prompt -------------------------------------------------------------
    def _prompt(
        self,
        pack: LocalePack,
        envelope: RequestEnvelope,
        norm: Any,
        answer: str | None,
        escalated: bool,
    ) -> str:
        p = pack.prompt
        lines = [p.raw_input.format(text=envelope.original_input.text)]
        if answer:
            lines.append(p.answer.format(answer=answer))
        hits = p.join.join(norm.alias_hits) if norm.alias_hits else p.none_label
        lines += [
            p.normalized_query.format(query=norm.normalized_query),
            p.alias_hits.format(hits=hits),
        ]
        if escalated:
            lines.append(p.escalation)
        lines.append(p.instructions.format(task_types=_TASK_TYPES))
        return "\n".join(lines)

    # -- assembly -----------------------------------------------------------
    def _assemble(
        self,
        envelope: RequestEnvelope,
        norm: Any,
        verdict: safety_gate.SafetyVerdict,
        *,
        answer: str | None,
        model: ModelInterpretation | None,
        model_name: str,
    ) -> FrontHalfOutput:
        now_ms = int(time.time() * 1000)
        model = model or ModelInterpretation()
        interpretation = InterpretationRecord(
            interpretation_id=new_id("int"),
            request_id=envelope.request_id,
            timestamp_ms=now_ms,
            # the deterministic pass owns the normalized query; the model
            # never rewrites text (input preservation rule)
            normalized_query=norm.normalized_query,
            task_type=model.task_type,
            deterministic=DeterministicSignals(**norm.as_signals()),
            model=ModelSignals(
                model_name=model_name,
                confidence=model.confidence,
                ambiguity_flags=list(model.ambiguity_flags),
                alternative_interpretations=list(model.alternative_interpretations),
            ),
            target_entity_guess=model.target_entity_guess,
            requested_attributes=list(model.requested_attributes),
            interpretation_summary=model.interpretation_summary,
        )
        constraints = dict(verdict.constraints)
        constraints.update(model.constraints)
        question = (
            verdict.question if verdict.decision == SafetyDecision.clarify_scope else ""
        )
        return FrontHalfOutput(
            interpretation=interpretation,
            safety=verdict.decision,
            action_type=verdict.action_type,
            risk=verdict.risk,
            missing_required_constraint=model.missing_required_constraint,
            user_resolvable_ambiguity=model.user_resolvable_ambiguity,
            clarification_question=question or model.clarification_question,
            constraints=constraints,
        )
