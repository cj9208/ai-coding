from __future__ import annotations

from research_agent.cli_support import _parse_answer, render_questions
from research_agent.contracts.models import Clarification, Option, Question

Q = Question(
    id="ques_1",
    gap_id="pg1",
    text="预算？",
    why_asking="F1",
    grounded_in=["F1"],
    options=[
        Option(value="low", label="3000 以内"),
        Option(value="high", label="可以上浮"),
    ],
)
CLAR = Clarification(questions=[Q], intro="两个问题")


class TestRender:
    def test_shows_why_and_skip_path(self):
        text = render_questions(CLAR)
        assert "为什么问：F1" in text and "跳过" in text and "3000 以内" in text


class TestParse:
    def test_indexed_choice(self):
        a = _parse_answer(Q, 1, "1.2")
        assert a.value == "high" and not a.skipped

    def test_bare_option_number(self):
        assert _parse_answer(Q, 1, "1").value == "low"

    def test_empty_is_skip(self):
        assert _parse_answer(Q, 1, "").skipped
        assert _parse_answer(Q, 1, "s").skipped

    def test_off_axis_free_text_kept_as_evidence(self):
        a = _parse_answer(Q, 1, "看情况，主要打游戏")
        assert a.value is None and a.free_text == "看情况，主要打游戏"
        assert not a.skipped
