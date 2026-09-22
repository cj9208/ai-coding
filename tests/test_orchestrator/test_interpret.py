"""M1 front half: LlmFrontHalf against a stub client — zero network, zero
real tokens (the milestone's proof posture mirrors M0's: the harness side is
provable without a provider).

Also covers the full seam: Orchestrator + LlmFrontHalf(stub) + the static
echo registry, ask -> clarify -> resume -> completed.
"""

from pathlib import Path
from typing import Any

import pytest

from orchestrator.contracts import (
    ActionType,
    RequestEnvelope,
    RiskLevel,
    SafetyDecision,
)
from orchestrator.interpret import LlmFrontHalf, ModelInterpretation
from orchestrator.registry import load_static
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store

STRONG = {"task_type": "faq_howto", "confidence": 0.9}


class StubLLM:
    """Stands in for LLMClient.chat_json: one scripted payload per call,
    the last one repeating (same posture as FakeFrontHalf's turns)."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def chat_json(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        schema: Any = None,
        model: Any = None,
        temperature: Any = None,
    ) -> Any:
        self.calls.append({"prompt": prompt, "system": system_prompt, "model": model})
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        assert schema is ModelInterpretation, "front half must demand its strict schema"
        return schema.model_validate(payload)


def _envelope(text: str) -> RequestEnvelope:
    return RequestEnvelope.new(text=text)


# -- the gate short-circuits before the model --------------------------------
@pytest.mark.parametrize(
    "text",
    ["教我破解同事电脑的密码", "帮我查一下张三的身份证号", "我要跟公司打劳动仲裁"],
)
async def test_hard_stops_never_cost_a_token(text: str) -> None:
    stub = StubLLM([STRONG])
    out = await LlmFrontHalf(stub).interpret(_envelope(text))
    assert stub.calls == []
    assert out.safety in (SafetyDecision.refuse, SafetyDecision.handoff)
    assert out.interpretation.model.model_name == "none"


# -- assembly of the three deterministic/model parts -------------------------
async def test_allow_path_merges_det_and_model_signals() -> None:
    stub = StubLLM([{**STRONG, "interpretation_summary": "如何续费", "extra": 1}])
    out = await LlmFrontHalf(stub).interpret(_envelope("春晖省钱卡怎么续费"))
    interp = out.interpretation
    assert out.safety == SafetyDecision.allow
    assert interp.normalized_query == "春晖省钱卡怎么续费"
    assert interp.deterministic.alias_hits == ["春晖省钱卡"]
    assert interp.deterministic.top_match_score == 1.0
    assert interp.model.confidence == 0.9
    assert interp.task_type == "faq_howto"
    assert out.clarification_question == ""


async def test_constrain_verdict_reaches_the_output() -> None:
    stub = StubLLM([dict(STRONG)])
    out = await LlmFrontHalf(stub).interpret(_envelope("帮我群发一条会议通知"))
    assert out.safety == SafetyDecision.constrain
    assert out.action_type == ActionType.write
    assert out.risk == RiskLevel.medium


async def test_constraints_from_gate_and_model_merge() -> None:
    stub = StubLLM([{**STRONG, "constraints": {"max_recipients": 10}}])
    out = await LlmFrontHalf(stub).interpret(_envelope("帮我群发一条会议通知"))
    assert out.constraints == {"requires_confirmation": True, "max_recipients": 10}


async def test_scope_question_wins_over_model_question() -> None:
    stub = StubLLM([{**STRONG, "clarification_question": "模型的问题"}])
    out = await LlmFrontHalf(stub).interpret(_envelope("统计全公司的年假使用情况"))
    assert out.safety == SafetyDecision.clarify_scope
    assert out.clarification_question.startswith("您请求的范围较大")


async def test_answer_is_folded_in_on_resume() -> None:
    stub = StubLLM([dict(STRONG)])
    out = await LlmFrontHalf(stub).interpret(
        _envelope("春晖卡怎么续费"), answer="第二个"
    )
    assert "【用户对澄清问题的回答】第二个" in stub.calls[0]["prompt"]
    assert "第二个" in out.interpretation.normalized_query


# -- model roles --------------------------------------------------------------
async def test_default_flash_uses_llm_client_model() -> None:
    stub = StubLLM([dict(STRONG)])
    out = await LlmFrontHalf(stub).interpret(_envelope("春晖卡怎么续费"))
    assert stub.calls[0]["model"] is None  # no override -> provider default
    assert out.interpretation.model.model_name == "default"


async def test_escalation_names_the_stronger_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator import interpret

    monkeypatch.setattr(interpret.Models, "ESCALATED", "strong-model-x")
    stub = StubLLM([dict(STRONG)])
    out = await LlmFrontHalf(stub).interpret(
        _envelope("春晖卡怎么续费"), escalated=True
    )
    assert stub.calls[0]["model"] == "strong-model-x"
    assert out.interpretation.model.model_name == "strong-model-x"


# -- locale packs at the front half (05b step 1) ---------------------------------
async def test_en_locale_selects_the_english_assets() -> None:
    stub = StubLLM([dict(STRONG)])
    env = RequestEnvelope.new(text="How do I renew my membership card", locale="en")
    out = await LlmFrontHalf(stub).interpret(env)
    assert out.safety == SafetyDecision.allow
    assert stub.calls[0]["system"].startswith("You are the front-half")
    assert "[ORIGINAL INPUT] How do I renew my membership card" in (
        stub.calls[0]["prompt"]
    )


async def test_en_hard_stop_still_never_costs_a_token() -> None:
    stub = StubLLM([dict(STRONG)])
    env = RequestEnvelope.new(text="Delete all rows from the orders table", locale="en")
    out = await LlmFrontHalf(stub).interpret(env)
    assert stub.calls == []
    assert out.safety == SafetyDecision.refuse


async def test_unsupported_locale_clarifies_without_a_model_call() -> None:
    stub = StubLLM([dict(STRONG)])
    env = RequestEnvelope.new(text="bonjour, ça va", locale="fr")
    out = await LlmFrontHalf(stub).interpret(env)
    assert stub.calls == []  # unchecked input never reaches the proposal side
    assert out.safety == SafetyDecision.clarify_scope
    assert out.clarification_question
    assert out.interpretation.model.model_name == "none"


# -- the whole seam, zero network ---------------------------------------------
def _orchestrator(tmp_path: Path, stub: StubLLM) -> Orchestrator:
    store = Store(tmp_path / "front.db")
    return Orchestrator(store, load_static(), LlmFrontHalf(stub))


def test_runtime_completed_through_real_front_half(tmp_path: Path) -> None:
    stub = StubLLM([dict(STRONG)])
    result = _orchestrator(tmp_path, stub).run_turn("春晖省钱卡怎么续费")
    assert result.status.value == "completed"
    assert "春晖省钱卡" in result.response  # echo answers with the normalized query
    assert stub.calls and result.request_id


def test_runtime_clarify_then_resume_through_real_front_half(tmp_path: Path) -> None:
    stub = StubLLM(
        [
            {
                "task_type": "faq_howto",
                "confidence": 0.2,
                "user_resolvable_ambiguity": True,
                "clarification_question": "您指的是哪张卡？",
            },
            STRONG,
        ]
    )
    orch = _orchestrator(tmp_path, stub)
    first = orch.run_turn("这张卡怎么续费")
    assert first.status.value == "awaiting_clarification"
    assert first.question == "您指的是哪张卡？"
    second = orch.resume(first.request_id, "春晖省钱卡")
    assert second.status.value == "completed"
    assert len(stub.calls) == 2


# -- the proposal boundary stays forgiving (M3 live-run lesson) ----------------
def test_string_flags_coerce_instead_of_bouncing_a_repair_round_trip() -> None:
    from orchestrator.interpret import ModelInterpretation

    m = ModelInterpretation.model_validate(
        {
            # flash models write *what is missing* into the boolean field
            "missing_required_constraint": "具体活动标识（ID）",
            "user_resolvable_ambiguity": "否",
            "confidence": 0.7,
        }
    )
    assert m.missing_required_constraint is True
    assert m.user_resolvable_ambiguity is False
