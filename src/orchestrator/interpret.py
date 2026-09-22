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
  embedded in an ASGI host);
- the **deterministic fast path** (05c step 3, ``config.FastPath``) is
  another proposer, not a bypass: it runs *after* the safety gate and
  normalize, synthesizes the same ``ModelInterpretation`` contract
  object from the alias evidence, and reports ``model_name="none"`` so
  the quota gate counts it as the zero-spend pass it is. The harness
  downstream cannot tell the difference — that is the parity contract.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import Field, field_validator

from . import safety as safety_gate
from .assess import STRONG_TOP_MATCH
from .config import FastPath, Models
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


def _synthesize(norm: Any) -> ModelInterpretation | None:
    """The fast path's zero-token proposal (05c step 3): eligible exactly
    when the deterministic pass alone carries strong evidence — one
    candidate at full specificity, the deterministic half of
    ``assess.strong_evidence``. Everything the model would add (attributes,
    questions, flags) is *absent because the input is unambiguous*, not
    because the fast path voted on it; the booleans it synthesizes are the
    same ones ``_assemble`` reads on the LLM path."""
    top = norm.top_match_score
    if norm.candidate_count != 1 or top is None or top < STRONG_TOP_MATCH:
        return None
    entity = norm.alias_hits[0]
    return ModelInterpretation(
        task_type=FastPath.TASK_TYPE,
        target_entity_guess=entity,
        interpretation_summary=f"deterministic alias hit: {entity}",
        confidence=1.0,
    )


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
        # the gate sees the *whole* user turn: a destructive fragment
        # arriving as a clarification answer must not bypass it
        # (05b step 4, cross-turn fragmentation)
        gated = f"{text} {answer}" if answer else text
        # locale is caller context (set by run_turn), never model output —
        # a proposal must not choose the table that gates it (05b)
        pack = get_pack(envelope.original_input.locale)
        if pack is None:
            # unchecked input: clarify without spending a token on an
            # interpretation the harness would not route anyway
            return self._assemble(
                envelope,
                normalize(text, aliases={}),
                safety_gate.evaluate(gated, envelope.original_input.locale),
                answer=answer,
                model=None,
                model_name="none",
            )
        verdict = safety_gate.evaluate(gated, pack.locale)
        if verdict.decision in (SafetyDecision.refuse, SafetyDecision.handoff):
            # hard stop: deterministic, before any probabilistic step
            norm = normalize(text, aliases=pack.aliases)
            return self._assemble(
                envelope, norm, verdict, answer=answer, model=None, model_name="none"
            )

        norm = normalize(f"{text} {answer}" if answer else text, aliases=pack.aliases)
        if (
            FastPath.ENABLED
            and not escalated
            and verdict.decision == SafetyDecision.allow
        ):
            # the gate and normalize ran first (DP-2); the fast path only
            # replaces the *proposal*, and an escalation pass exists precisely
            # because the model is wanted
            synthesized = _synthesize(norm)
            if synthesized is not None:
                return self._assemble(
                    envelope,
                    norm,
                    verdict,
                    answer=answer,
                    model=synthesized,
                    model_name="none",
                )
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
        # the gate's constraints win (05b step 4): a proposal cannot talk
        # the harness out of a confirmation the safety table demanded
        constraints = {**model.constraints, **verdict.constraints}
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
