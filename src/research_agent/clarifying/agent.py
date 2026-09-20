"""ClarifyingAgent (03): evidence gaps are never shown to it as candidates;
the LLM only ever sees preference gaps, and the validator re-checks that no
routing error crept through.

Cost rule: if there are zero candidate preference gaps left, we do not spend
an LLM call inventing questions — nothing_to_ask is returned by code.
"""

from __future__ import annotations

import logging

from ..contracts.models import (
    Clarification,
    ClarificationDraft,
    PhaseName,
)
from ..contracts.prompts import render_prompt
from ..orchestrator.budget import Budget, BudgetView
from ..orchestrator.machine import PhaseOutcome
from ..orchestrator.view import SessionView
from ..persistence.store import SessionStore
from ..research.agent import synthesize_pack
from .validator import validate_questions

logger = logging.getLogger(__name__)


class ClarifyingAgent:
    name = "clarifying"

    def __init__(self, llm, store: SessionStore):
        self.llm = llm
        self.store = store

    async def run(self, view: SessionView, budget: BudgetView) -> PhaseOutcome:
        pack = view.latest_pack or synthesize_pack(view, stop_reason=view.stop_reason)
        brief = view.require_brief()
        already_asked = view.asked_gap_ids()
        candidates = [g for g in pack.preference_gaps if g.id not in already_asked]

        if not candidates:
            return self._nothing_to_ask(view, pack)

        prompt = render_prompt(
            "clarify",
            TOPIC=brief.topic,
            USER_CONTEXT=brief.user_context or "(none)",
            PRIOR_ANSWERS=_prior_answers(view),
            PREFERENCE_GAPS="; ".join(
                f"{g.id}: {g.description} [blocks: {','.join(g.blocked_criteria) or '-'}]"
                for g in candidates
            ),
            FINDING_DIGEST=_finding_digest(view.findings),
            MAX_QUESTIONS=str(_max_q(budget)),
            LANGUAGE="Chinese" if brief.language.startswith("zh") else "English",
        )
        draft: ClarificationDraft = await self.llm.chat_json(
            prompt, schema=ClarificationDraft, temperature=0.3
        )

        for i, q in enumerate(draft.questions, 1):
            q.id = q.id or f"ques_{i}"
        report = validate_questions(
            draft.questions,
            finding_ids=view.finding_ids(),
            evidence_gap_ids={g.id for g in pack.evidence_gaps},
            asked_gap_ids=already_asked,
            max_questions=_max_q(budget),
            allowed_gap_ids={g.id for g in candidates},
        )
        for gap_id, reason in report.dropped:
            logger.warning("question for %s dropped: %s", gap_id, reason)

        if not report.kept:
            return self._nothing_to_ask(view, pack)

        clarification = Clarification(
            session_id=view.session_id,
            questions=report.kept,
            intro=draft.intro,
            round=self._round(view),
        )
        for q in report.kept:
            self.store.gap_event(
                view.session_id, "preference", q.gap_id, "asked", ref=q.id
            )
        return PhaseOutcome(
            next_phase=PhaseName.AWAIT_USER,
            artifacts=[clarification],
            bumps={"clarify_rounds": 1},
        )

    def _nothing_to_ask(self, view: SessionView, pack) -> PhaseOutcome:
        """First-class, common output: short-circuit to RECOMMEND (03 §5)."""
        return PhaseOutcome(
            next_phase=PhaseName.RECOMMEND,
            artifacts=[
                Clarification(
                    session_id=view.session_id,
                    questions=[],
                    intro="研究已覆盖决策所需信息，无需追问。",
                    round=self._round(view),
                )
            ],
        )

    @staticmethod
    def _round(view: SessionView) -> int:
        return len(view.clarifications) + 1


def _max_q(budget: BudgetView) -> int:
    return Budget().max_questions  # MVP: fixed policy; v1 wires per-session budgets


def _prior_answers(view: SessionView) -> str:
    qmap = {q.id: q for c in view.clarifications for q in c.questions}
    lines = []
    for ua in view.answers:
        for a in ua.answers:
            q = qmap.get(a.question_id)
            state = "SKIPPED" if a.skipped else (a.value or a.free_text or "")
            lines.append(f"{a.question_id} ({q.text if q else '?'}): {state}")
    return "\n".join(lines) or "(none)"


def _finding_digest(findings) -> str:
    lines = [
        f"{f.id} | {f.claim[:160]} | {','.join(f.touches_candidates) or '-'} | "
        f"{','.join(f.decision_criteria) or '-'}"
        for f in findings[:80]
    ]
    return "\n".join(lines) or "(no findings)"
