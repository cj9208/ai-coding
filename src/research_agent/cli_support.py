"""CLI wiring: dependency assembly + the human-in-the-loop prompt surface.

Kept separate from cli.py so tests can drive the answer format (rendering and
parsing) without stdin gymnastics.
"""

from __future__ import annotations

import sys
from pathlib import Path

from llm_client import get_client

from .clarifying.agent import ClarifyingAgent
from .contracts.models import Clarification, Question, QuestionAnswer, UserAnswers
from .orchestrator import Orchestrator
from .orchestrator.budget import Budget
from .recommendation.agent import RecommendationAgent
from .research import ResearchAgent
from .research.adapters import FakeAdapter, default_adapters
from .storage import SessionStore


def build_orchestrator(
    store: SessionStore, *, fake_dir: str = "", max_collect: int = 30
) -> Orchestrator:
    llm = get_client()
    adapters = (
        {"web_search": FakeAdapter(Path(fake_dir))} if fake_dir else default_adapters()
    )
    return Orchestrator(
        store,
        research=ResearchAgent(llm, adapters, store),
        clarifying=ClarifyingAgent(llm, store),
        recommender=RecommendationAgent(llm, store),
        budget=Budget(max_collect_calls=max_collect),
    )


# -- rendering / collecting questions (03 §4: ONE message, never a drip) ------


def render_questions(clar: Clarification | None) -> str:
    if not clar or not clar.questions:
        return ""
    lines = [clar.intro or "基于已收集的证据，有几个问题会让推荐更准：", ""]
    for i, q in enumerate(clar.questions, 1):
        lines.append(f"{i}. {q.text}")
        if q.why_asking:
            lines.append(f"   为什么问：{q.why_asking}")
        for j, o in enumerate(q.options, 1):
            lines.append(f"     [{i}.{j}] {o.label}")
        lines.append(f"     [{i}.s] 跳过 / 直接输入其他想法")
    return "\n".join(lines)


def collect_answers(
    clar: Clarification, *, use_editor: bool = False, stdin=sys.stdin
) -> UserAnswers:
    """One prompt per question; a non-answer is a skip, free text is evidence
    about preferences (01 §5). Never re-asks: that is the orchestrator's job."""
    answers: list[QuestionAnswer] = []
    print("（输入如 1.2 选择项；直接输入文字作为补充想法；回车或 s 跳过）")
    for i, q in enumerate(clar.questions, 1):
        raw = _ask(stdin, f"{i}. {q.text}")
        answers.append(_parse_answer(q, i, raw))
    comment = _ask(stdin, "还有什么整体补充？（回车跳过）")
    return UserAnswers(answers=answers, global_comment=comment.strip())


def _ask(stdin, prompt: str) -> str:
    try:
        return input(prompt + "\n> ").strip()
    except EOFError:
        return ""


def _parse_answer(q: Question, index: int, raw: str) -> QuestionAnswer:
    qid = q.id
    if not raw or raw.lower() in {"s", "skip", "跳过"}:
        return QuestionAnswer(question_id=qid, skipped=True)
    # "1.2" style selection
    if "." in raw:
        head, _, tail = raw.partition(".")
        if head.strip() == str(index) and tail.strip().isdigit():
            j = int(tail.strip()) - 1
            if 0 <= j < len(q.options):
                return QuestionAnswer(question_id=qid, value=q.options[j].value)
    # bare option number also accepted
    if raw.isdigit() and 1 <= int(raw) <= len(q.options):
        return QuestionAnswer(question_id=qid, value=q.options[int(raw) - 1].value)
    # off-axis / free-text: preference evidence, read by the profile builder
    return QuestionAnswer(question_id=qid, value=_match_option(q, raw), free_text=raw)


def _match_option(q: Question, raw: str) -> str | None:
    low = raw.lower()
    for o in q.options:
        if low == o.value.lower() or low == o.label.lower():
            return o.value
    return None
