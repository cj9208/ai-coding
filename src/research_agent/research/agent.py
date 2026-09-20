"""ResearchAgent: the Research subagent as four orchestrator steps
(PLAN / COLLECT / REFLECT / DEEPEN), one shared code path for phase-1 and
targeted collection (02 intro).

It never advances the phase: each step returns a PhaseOutcome telling the
machine where to go and what it produced. Capture/Finding rows and gap
events are append-only ledger writes made mid-step (documented relaxation
in machine.py); all STATE moves go through the orchestrator.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Optional

from ..contracts.models import (
    BatchReport,
    EvidenceGap,
    EvidencePack,
    Finding,
    GapDraft,
    PhaseName,
    ReflectDecision,
    SourceFailure,
    SubQuery,
    UserAnswers,
)
from ..orchestrator.budget import BudgetView
from ..orchestrator.machine import PhaseOutcome
from ..orchestrator.view import SessionView
from ..persistence.store import SessionStore
from .adapters.base import SourceAdapter
from .collector import Collector
from .planner import Planner
from .reflector import SATURATION_STREAK, Reflector, batch_novelty

logger = logging.getLogger(__name__)

COVERAGE_FLOOR = {"good", "adequate"}


class ResearchAgent:
    name = "research"

    def __init__(self, llm, adapters: Mapping[str, SourceAdapter], store: SessionStore):
        self.llm = llm
        self.store = store
        self.planner = Planner(llm)
        self.collector = Collector(llm, adapters, store)
        self.reflector = Reflector(llm)

    # -- PLAN -----------------------------------------------------------------

    async def plan_step(self, view: SessionView, budget: BudgetView) -> PhaseOutcome:
        brief = view.require_brief()
        plan = await self.planner.make_plan(brief, budget.collect_left)
        return PhaseOutcome(next_phase=PhaseName.COLLECT, artifacts=[plan])

    # -- COLLECT -----------------------------------------------------------------

    async def collect_step(self, view: SessionView, budget: BudgetView) -> PhaseOutcome:
        brief = view.require_brief()
        plan = view.plans[-1]
        batch_no = self._next_batch(view)
        queries = self._schedule(plan.sub_queries, budget.collect_left)
        outcome = await self.collector.run_batch(
            brief,
            queries,
            existing_findings=view.findings,
            batch_no=batch_no,
            calls_left=budget.collect_left,
        )
        report = BatchReport(
            batch=batch_no,
            calls_used=outcome.calls_used,
            quotes_rejected=outcome.quotes_rejected,
            captures_new=outcome.captures_new,
            captures_deduped=outcome.captures_deduped,
            new_finding_ids=[f.id for f in outcome.findings],
            failures=outcome.failures,
        )
        return PhaseOutcome(
            next_phase=PhaseName.REFLECT,
            artifacts=[report],
            bumps={"collect_calls": outcome.calls_used},
        )

    # -- REFLECT -----------------------------------------------------------------

    async def reflect_step(self, view: SessionView, budget: BudgetView) -> PhaseOutcome:
        brief = view.require_brief()
        batch_no = max((f.batch for f in view.findings), default=0)
        novelty = batch_novelty(view.findings, batch_no) if view.findings else None
        low_streak = self._low_novelty_streak(view)

        prev = view.latest(ReflectDecision)
        e_gaps = list(prev.evidence_gaps) if prev else []
        p_gaps = (
            list(prev.preference_gaps)
            if prev
            else [
                # seeds from the planner flow into the reflector's view (02 §2)
                GapDraft(description=g.description, blocked_criteria=g.blocked_criteria)
                for g in (
                    view.plans[-1].hypothesized_preference_gaps if view.plans else []
                )
            ]
        )
        max_gap_id = self._max_gap_id(view, e_gaps, p_gaps)

        decision = await self.reflector.decide(
            brief,
            intents=(
                sorted({q.intent for q in view.plans[-1].sub_queries})
                if view.plans
                else []
            ),
            findings=view.findings,
            failures=[f for r in view.batch_reports for f in r.failures],
            prev_evidence_gaps=e_gaps,
            prev_preference_gaps=p_gaps,
            batch_histories=self._batch_histories(view),
            remaining_calls=budget.collect_left,
            max_gap_id=max_gap_id,
        )
        # routing hygiene the prompt asks for, code guarantees:
        for g in decision.preference_gaps:
            g.candidate_queries = []
        self._record_gap_events(view, decision)

        stop_reason = self._stop_decision(view, decision, low_streak, novelty, budget)
        if stop_reason is None:
            # continue: hints go BACK to the planner, never executed directly (02 §4)
            hints = "; ".join(decision.next_queries_hint)
            replan = await self.planner.make_plan(
                brief,
                budget.collect_left,
                max_queries=4,
                deepen_hint=(
                    (
                        "Revise the plan to close these gaps; hints from "
                        "reflection:\n" + hints
                    )
                    if hints
                    else ""
                ),
            )
            return PhaseOutcome(
                next_phase=PhaseName.COLLECT,
                artifacts=[decision, replan],
                bumps={"research_iters": 1},
            )

        pack = self.build_pack(view, decision, stop_reason)
        return PhaseOutcome(
            next_phase=PhaseName.CLARIFY,
            artifacts=[decision, pack],
            bumps={"research_iters": 1},
            stop_reason=stop_reason,
        )

    # -- DEEPEN -----------------------------------------------------------------

    async def deepen_step(self, view: SessionView, budget: BudgetView) -> PhaseOutcome:
        brief = view.require_brief()
        answers = view.answers[-1] if view.answers else None
        pack = view.latest_pack or synthesize_pack(view)
        hint = self._deepen_hint(view, pack, answers)
        if not hint:
            return PhaseOutcome(next_phase=PhaseName.RECOMMEND)

        plan = await self.planner.make_plan(
            brief, budget.deepen_left, max_queries=4, deepen_hint=hint
        )
        queries = [q for q in plan.sub_queries if q.priority == 1]
        if not queries:
            return PhaseOutcome(next_phase=PhaseName.RECOMMEND, artifacts=[plan])
        batch_no = self._next_batch(view)
        outcome = await self.collector.run_batch(
            brief,
            queries,
            existing_findings=view.findings,
            batch_no=batch_no,
            calls_left=budget.deepen_left,
        )
        report = BatchReport(
            batch=batch_no,
            calls_used=outcome.calls_used,
            quotes_rejected=outcome.quotes_rejected,
            captures_new=outcome.captures_new,
            captures_deduped=outcome.captures_deduped,
            new_finding_ids=[f.id for f in outcome.findings],
            failures=outcome.failures,
        )
        new_pack = self.build_pack(
            view,
            decision_from_pack=pack,
            stop_reason=pack.stop_reason,
            extra_failures=outcome.failures,
            extra_report=report,
            answers=answers,
        )
        bumps = {"deepen_calls": outcome.calls_used}
        if outcome.calls_used >= budget.deepen_left:
            return PhaseOutcome(
                next_phase=PhaseName.RECOMMEND,
                artifacts=[plan, report, new_pack],
                bumps=bumps,
                stop_reason="budget",
            )
        return PhaseOutcome(
            next_phase=PhaseName.RECOMMEND,
            artifacts=[plan, report, new_pack],
            bumps=bumps,
        )

    # -- helpers -----------------------------------------------------------------

    @staticmethod
    def _schedule(queries: list[SubQuery], calls_left: int) -> list[SubQuery]:
        """Priority 1 first; tier 2 only if there is real room (~7 calls/query).
        The planner sized the plan to the budget; this keeps that promise."""
        scheduled = [q for q in queries if q.priority == 1]
        room = calls_left - 7 * len(scheduled)
        scheduled += [q for q in queries if q.priority == 2 and room > 0]
        return scheduled

    @staticmethod
    def _next_batch(view: SessionView) -> int:
        return max((r.batch for r in view.batch_reports), default=0) + 1

    @staticmethod
    def _batch_histories(view: SessionView) -> list[str]:
        return [
            f"batch {r.batch}: {len(r.new_finding_ids)} findings, "
            f"{r.calls_used} calls, {r.quotes_rejected} quotes rejected"
            for r in view.batch_reports
        ]

    @staticmethod
    def _low_novelty_streak(view: SessionView) -> int:
        batches = sorted({f.batch for f in view.findings})
        streak = 0
        for b in reversed(batches):
            if batch_novelty(view.findings, b).low:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _max_gap_id(view: SessionView, e_gaps, p_gaps) -> int:
        ids = [
            g.id for d in view.decisions for g in d.evidence_gaps + d.preference_gaps
        ]
        ids += [g.id for g in e_gaps + p_gaps]
        nums = [int(i[2:]) for i in ids if i and i[2:].isdigit()]
        return max(nums, default=0)

    def _record_gap_events(self, view: SessionView, decision: ReflectDecision) -> None:
        prev = view.latest(ReflectDecision)
        prev_ids = (
            {g.id for g in prev.evidence_gaps} | {g.id for g in prev.preference_gaps}
            if prev
            else set()
        )
        now = [(g, "evidence") for g in decision.evidence_gaps] + [
            (g, "preference") for g in decision.preference_gaps
        ]
        now_ids = {g.id for g, _ in now}
        for g, kind in now:
            if g.id not in prev_ids:
                self.store.gap_event(view.session_id, kind, g.id, "open")
        for gid in prev_ids - now_ids:
            kind = "evidence" if gid.startswith("eg") else "preference"
            self.store.gap_event(
                view.session_id,
                kind,
                gid,
                "resolved",
                ref=f"batch-{self._next_batch(view) - 1}",
            )

    def _stop_decision(
        self, view, decision, low_streak, novelty, budget
    ) -> Optional[str]:
        """02 §4 stop conditions; None means keep researching. Budget first:
        if it is already spent, "saturated" would be a polite lie."""
        if budget is not None and budget.collect_left <= 0:
            return "budget"
        if low_streak >= SATURATION_STREAK or decision.saturated:
            return "saturated"
        if not view.findings:
            return "budget"
        coverage = decision.coverage
        if (
            view.findings
            and coverage
            and all(
                coverage.get(i, "missing") in COVERAGE_FLOOR
                for q in (view.plans[-1].sub_queries if view.plans else [])
                for i in [q.intent]
            )
        ):
            return "coverage-floor"
        if not decision.continue_researching:
            return decision.reason[:80] or "reflector-stop"
        return None

    @staticmethod
    def _deepen_hint(
        view: SessionView, pack: EvidencePack, answers: UserAnswers | None
    ) -> str:
        """What the answers made relevant (00 §2): option texts + free text +
        candidate_queries from still-open evidence gaps. Empty => nothing to
        chase, skip the second collection entirely."""
        parts: list[str] = []
        if answers:
            qmap = {
                q.id: q
                for q in (view.clarification.questions if view.clarification else [])
            }
            for a in answers.answers:
                if a.skipped:
                    continue
                q = qmap.get(a.question_id)
                label = a.free_text or ""
                if a.value and q:
                    opt = next(
                        (o.label for o in q.options if o.value == a.value), a.value
                    )
                    label = f"{opt}. {label}".strip(". ")
                if label:
                    parts.append(
                        f"User said ({q.text if q else a.question_id}): {label}"
                    )
            if answers.global_comment:
                parts.append(f"User added: {answers.global_comment}")
        open_queries = [
            cq for g in pack.evidence_gaps for cq in g.candidate_queries[:2]
        ]
        if open_queries:
            parts.append("Open evidence questions: " + "; ".join(open_queries[:4]))
        return "\n".join(parts)

    def build_pack(
        self,
        view: SessionView,
        decision: ReflectDecision | None = None,
        stop_reason: str | None = None,
        *,
        decision_from_pack: EvidencePack | None = None,
        extra_failures: list[SourceFailure] | None = None,
        extra_report: BatchReport | None = None,
        answers: UserAnswers | None = None,
    ) -> EvidencePack:
        findings: list[Finding] = list(view.findings)
        if decision is not None:
            e_gaps = [
                EvidenceGap(
                    id=g.id,
                    kind="evidence",
                    description=g.description,
                    blocked_criteria=g.blocked_criteria,
                    candidate_queries=g.candidate_queries,
                    session_id=view.session_id,
                )
                for g in decision.evidence_gaps
            ]
            p_gaps = [
                EvidenceGap(
                    id=g.id,
                    kind="preference",
                    description=g.description,
                    blocked_criteria=g.blocked_criteria,
                    session_id=view.session_id,
                )
                for g in decision.preference_gaps
            ]
            coverage = dict(decision.coverage)
        else:
            src = decision_from_pack or view.latest_pack
            e_gaps, p_gaps = (
                (src.evidence_gaps, src.preference_gaps) if src else ([], [])
            )
            coverage = dict(src.coverage) if src else {}
        if answers and view.clarification:
            answered = {a.question_id for a in answers.answers if not a.skipped}
            closed = {
                q.gap_id for q in view.clarification.questions if q.id in answered
            } | (
                {q.gap_id for q in view.clarification.questions}
                if answers.global_comment
                else set()
            )
            # answered gaps close; skipped ones become ASSUMPTIONS downstream
            # (03 §5) — handled by the recommender, not kept as open questions
            p_gaps = [g for g in p_gaps if g.id not in closed]
        failures = [f for r in view.batch_reports for f in r.failures]
        if extra_report:
            failures += extra_report.failures
        if extra_failures:
            failures += extra_failures
        return EvidencePack(
            session_id=view.session_id,
            findings=findings,
            evidence_gaps=e_gaps,
            preference_gaps=p_gaps,
            source_failures=failures,
            coverage=coverage,
            stop_reason=stop_reason,
        )


def synthesize_pack(view: SessionView, stop_reason: str | None = None) -> EvidencePack:
    """Fallback when the machine force-advanced without the research agent
    assembling a pack (budget violation): rebuild from stored artifacts so
    CLARIFY/RECOMMEND always have inputs (01 §3: degrade, never discard)."""
    latest = view.latest_pack
    if latest:
        return latest.model_copy(
            update={"stop_reason": latest.stop_reason or stop_reason}
        )
    decision = view.latest(ReflectDecision)
    e_gaps = [
        EvidenceGap(
            id=g.id,
            kind="evidence",
            description=g.description,
            blocked_criteria=g.blocked_criteria,
            candidate_queries=g.candidate_queries,
        )
        for g in (decision.evidence_gaps if decision else [])
    ]
    p_gaps = [
        EvidenceGap(
            id=g.id,
            kind="preference",
            description=g.description,
            blocked_criteria=g.blocked_criteria,
        )
        for g in (decision.preference_gaps if decision else [])
    ]
    return EvidencePack(
        session_id=view.session_id,
        findings=list(view.findings),
        evidence_gaps=e_gaps,
        preference_gaps=p_gaps,
        source_failures=[f for r in view.batch_reports for f in r.failures],
        coverage=dict(decision.coverage) if decision else {},
        stop_reason=stop_reason or "budget",
    )
