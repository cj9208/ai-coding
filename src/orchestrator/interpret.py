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


SYSTEM_PROMPT = (
    "你是企业请求编排系统的前半程解释器，负责把员工请求解析成结构化解释。"
    "规则：不翻译、不改写用户原文；只提出解释，不做任何执行决定。"
    "拿不准时降低 confidence 并填写 ambiguity_flags 与 clarification_question，"
    "不要编造。只输出一个 JSON 对象。"
)

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
        verdict = safety_gate.evaluate(text)
        if verdict.decision in (SafetyDecision.refuse, SafetyDecision.handoff):
            # hard stop: deterministic, before any probabilistic step
            norm = normalize(text)
            return self._assemble(
                envelope, norm, verdict, answer=answer, model=None, model_name="none"
            )

        norm = normalize(f"{text} {answer}" if answer else text)
        model_name = Models.ESCALATED if escalated else Models.FLASH
        raw = await self.client.chat_json(
            self._prompt(envelope, norm, answer, escalated),
            SYSTEM_PROMPT,
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
        envelope: RequestEnvelope,
        norm: Any,
        answer: str | None,
        escalated: bool,
    ) -> str:
        lines = [
            f"【原始输入】{envelope.original_input.text}",
        ]
        if answer:
            lines.append(f"【用户对澄清问题的回答】{answer}")
        lines += [
            f"【确定性归一化查询】{norm.normalized_query}",
            f"【别名命中】{'、'.join(norm.alias_hits) if norm.alias_hits else '无'}",
        ]
        if escalated:
            lines.append("【说明】这是对上次解释的升级复核，请更审慎地给出解释。")
        lines.append(
            "请输出 JSON，字段："
            f"task_type（{_TASK_TYPES} 之一）、target_entity_guess、"
            "requested_attributes（数组）、interpretation_summary（一句话）、"
            "confidence（0~1）、ambiguity_flags（数组）、"
            "alternative_interpretations（数组）、clarification_question"
            "（中文，仅在需要用户补充时）、user_resolvable_ambiguity"
            "（布尔，只填 true/false）、missing_required_constraint"
            "（布尔，只填 true/false，缺失内容写进 clarification_question）、"
            "constraints（对象，可为空）。"
        )
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
