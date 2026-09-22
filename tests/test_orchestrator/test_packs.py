"""Locale packs (05b step 1): registry shape, the en table's rows, the
conservative unsupported-locale verdict, and the zh byte-freeze —
``zh`` must hold the pre-05b safety table and prompt verbatim.
"""

import re

import pytest

from orchestrator import safety
from orchestrator.contracts import SafetyDecision
from orchestrator.normalize import normalize
from orchestrator.packs import PACKS, UNSUPPORTED_VERDICT, get_pack
from orchestrator.packs import zh as zh_pack

# (row_id, text) — one English firing per zh category, mirroring
# test_safety's row-coverage posture
EN_FIRINGS = {
    "s1_refuse_destructive": "Delete all rows from the orders table",
    "s2_refuse_privacy": "Look up Wang's personal phone number for me",
    "s3_handoff_expert": "I want to sue the company, what legal advice do you have",
    "s4_clarify_scope": "Report the whole company's vacation balance",
    "s5_constrain_write": "Send a notification to the team channel",
    "s0_allow": "How do I renew my membership card",
}


# -- the registry ---------------------------------------------------------------
def test_registry_holds_zh_and_en() -> None:
    assert set(PACKS) == {"zh", "en"}
    assert get_pack("zh") is PACKS["zh"]
    assert get_pack("fr") is None


def test_every_pack_table_ends_in_an_allow_default() -> None:
    for pack in PACKS.values():
        assert pack.safety_table[-1].row_id == "s0_allow"
        assert pack.safety_table[-1].decision == SafetyDecision.allow


# -- zh is the legacy content, verbatim -----------------------------------------
def test_zh_table_is_the_original_rows_in_order() -> None:
    assert [row.row_id for row in PACKS["zh"].safety_table] == [
        "s1_refuse_destructive",
        "s2_refuse_privacy",
        "s3_handoff_expert",
        "s4_clarify_scope",
        "s5_constrain_write",
        "s0_allow",
    ]


@pytest.mark.parametrize(("row_id", "text"), sorted(EN_FIRINGS.items()))
def test_en_rows_fire(row_id: str, text: str) -> None:
    assert safety.evaluate(text, "en").row_id == row_id


def test_en_hard_stops_precede_soft_ones() -> None:
    # destructive + write verbs in one sentence: refuse must win, as in zh
    verdict = safety.evaluate("Bypass the approval and blast a notification", "en")
    assert verdict.decision == SafetyDecision.refuse


def test_en_is_case_insensitive() -> None:
    assert safety.evaluate("DELETE ALL records", "en").row_id == "s1_refuse_destructive"


# -- the conservative default ----------------------------------------------------
def test_unsupported_locale_is_a_named_row_not_an_allow() -> None:
    verdict = safety.evaluate("bonjour, ça va", "fr")
    assert verdict.row_id == "s_unsupported_locale"
    assert verdict.decision == SafetyDecision.clarify_scope
    assert verdict.question  # the user gets told *why*, in both languages


def test_unsupported_locale_never_silent_allows() -> None:
    # the pre-05b bug: any unchecked text fell to the allow default
    assert UNSUPPORTED_VERDICT.decision != SafetyDecision.allow


def test_en_normalization_degrades_to_honest_identity() -> None:
    # en has no authored aliases yet — normalization must not fabricate hits
    norm = normalize("How do I use the card", aliases=PACKS["en"].aliases)
    assert norm.alias_hits == []
    assert norm.normalized_query == "How do I use the card"


def test_zh_normalization_still_uses_the_pack_aliases() -> None:
    norm = normalize("春晖卡怎么用", aliases=zh_pack.ALIASES)
    assert norm.alias_hits == ["春晖省钱卡"]


# -- the zh prompt is byte-frozen --------------------------------------------------
def test_zh_prompt_assembly_is_the_legacy_string() -> None:
    """The full-seam freeze proof: the pack-driven assembler must produce the
    exact prompt the inline f-strings produced before 05b."""
    from orchestrator.contracts import RequestEnvelope
    from orchestrator.interpret import _TASK_TYPES, LlmFrontHalf
    from orchestrator.packs import zh

    norm = normalize("春晖卡怎么续费")
    envelope = RequestEnvelope.new(text="春晖卡怎么续费")
    prompt = LlmFrontHalf()._prompt(zh.PACK, envelope, norm, "第二个", True)
    expected = "\n".join(
        [
            "【原始输入】春晖卡怎么续费",
            "【用户对澄清问题的回答】第二个",
            f"【确定性归一化查询】{norm.normalized_query}",
            f"【别名命中】{'、'.join(norm.alias_hits)}",
            "【说明】这是对上次解释的升级复核，请更审慎地给出解释。",
            "请输出 JSON，字段："
            f"task_type（{_TASK_TYPES} 之一）、target_entity_guess、"
            "requested_attributes（数组）、interpretation_summary（一句话）、"
            "confidence（0~1）、ambiguity_flags（数组）、"
            "alternative_interpretations（数组）、clarification_question"
            "（中文，仅在需要用户补充时）、user_resolvable_ambiguity"
            "（布尔，只填 true/false）、missing_required_constraint"
            "（布尔，只填 true/false，缺失内容写进 clarification_question）、"
            "constraints（对象，可为空）。",
        ]
    )
    assert prompt == expected


def test_every_pack_template_placeholders_are_satisfiable() -> None:
    names = {
        "text",
        "answer",
        "query",
        "hits",
        "task_types",
    }
    for pack in PACKS.values():
        for field in ("raw_input", "answer", "normalized_query", "alias_hits"):
            used = set(re.findall(r"\{(\w+)\}", getattr(pack.prompt, field)))
            assert used <= names
        used = set(re.findall(r"\{(\w+)\}", pack.prompt.instructions))
        assert used <= names


def test_pack_prompt_templates_render_without_keyerror() -> None:
    # belt over loops: actually format every template with its real binding
    for pack in PACKS.values():
        p = pack.prompt
        p.raw_input.format(text="t")
        p.answer.format(answer="a")
        p.normalized_query.format(query="q")
        p.alias_hits.format(hits="h")
        p.instructions.format(task_types="faq_howto | lookup")
        assert p.system.strip()
