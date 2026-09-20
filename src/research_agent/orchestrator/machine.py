"""The state machine (01 §2): the ONLY place transitions are written and the
only module allowed to advance session.phase.

Contract with subagents (01 §6): they are pure functions of input artifacts,
return their outputs in a PhaseOutcome, and never touch phase themselves.
Two deliberate relaxations, both in service of resumability:
- The collector appends captures/findings through an injected recorder during
  its run: capture dedup (UNIQUE url+hash) and stable F*/C* ids need
  incremental writes. These are append-only ledger rows, not state moves.
- Budget consumption is reported in the outcome and committed in the SAME
  transaction as the artifacts (05 §4).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from ..contracts.models import PhaseName, UserAnswers
from ..storage.store import SessionStore
from .budget import Budget, BudgetView
from .view import ArtifactError, SessionView

logger = logging.getLogger(__name__)


@dataclass
class PhaseOutcome:
    """What a phase reports back: where to go, what it produced, what it spent."""

    next_phase: PhaseName
    artifacts: list[BaseModel] = field(default_factory=list)
    bumps: dict[str, int] = field(default_factory=dict)
    #: set when the phase ran out of something and degraded instead of failing
    stop_reason: str | None = None


class Phase(Protocol):
    name: str

    async def run(self, view: SessionView, budget: BudgetView) -> PhaseOutcome: ...


class ResearchPhase(Protocol):
    """The research agent serves four phases with four different steps;
    the machine dispatches to the method the current phase names."""

    name: str

    async def plan_step(
        self, view: SessionView, budget: BudgetView
    ) -> PhaseOutcome: ...

    async def collect_step(
        self, view: SessionView, budget: BudgetView
    ) -> PhaseOutcome: ...

    async def reflect_step(
        self, view: SessionView, budget: BudgetView
    ) -> PhaseOutcome: ...

    async def deepen_step(
        self, view: SessionView, budget: BudgetView
    ) -> PhaseOutcome: ...


#: phases where a step is executed by a subagent
EXECUTED = {
    PhaseName.PLAN,
    PhaseName.COLLECT,
    PhaseName.REFLECT,
    PhaseName.DEEPEN,
    PhaseName.CLARIFY,
    PhaseName.RECOMMEND,
}
TERMINAL = {PhaseName.DONE, PhaseName.FAILED, PhaseName.AWAIT_USER}

StepFn = Callable[[SessionView, BudgetView], Awaitable[PhaseOutcome]]


class Orchestrator:
    def __init__(
        self,
        store: SessionStore,
        *,
        research: ResearchPhase,
        clarifying: Phase,
        recommender: Phase,
        budget: Budget | None = None,
    ):
        self.store = store
        self.budget = budget or Budget()
        # Phases map to executor METHODS, not a generic run().
        self._steps: dict[PhaseName, StepFn] = {
            PhaseName.PLAN: research.plan_step,
            PhaseName.COLLECT: research.collect_step,
            PhaseName.REFLECT: research.reflect_step,
            PhaseName.DEEPEN: research.deepen_step,
            PhaseName.CLARIFY: clarifying.run,
            PhaseName.RECOMMEND: recommender.run,
        }

    # -- public API -----------------------------------------------------------

    async def run(self, session_id: str) -> PhaseName:
        """Advance until AWAIT_USER (HITL pause), DONE, or FAILED."""
        started = time.monotonic()
        self._init_counters(session_id)
        while True:
            phase = PhaseName(self.store.get_phase(session_id))
            if phase in TERMINAL:
                return phase
            if phase is PhaseName.INTAKE:
                self._leave_intake(session_id)
                continue
            if time.monotonic() - started > self.budget.wallclock_seconds:
                logger.warning("wallclock budget exceeded; forcing forward")
                self._force_advance(session_id, phase, "budget")
                continue
            if not self._budget_allows(session_id, phase):
                self._force_advance(session_id, phase, "budget")
                continue
            outcome = await self._run_step(session_id, phase)
            if outcome is None:  # hard failure already recorded
                return PhaseName(self.store.get_phase(session_id))
        # unreachable; loop returns from TERMINAL

    def resume(self, session_id: str, answers: UserAnswers) -> None:
        """Re-enter the machine at DEEPEN with the answers appended (01 §4)."""
        self.store.transition(session_id, PhaseName.DEEPEN.value, artifacts=[answers])

    # -- internals --------------------------------------------------------------

    def _leave_intake(self, session_id: str) -> None:
        view = self.load_view(session_id)
        if view.brief is None or not view.brief.topic.strip():
            self.store.transition(
                session_id, PhaseName.FAILED.value, stop_reason="invalid brief"
            )
        else:
            self.store.transition(session_id, PhaseName.PLAN.value)

    async def _run_step(self, session_id: str, phase: PhaseName) -> PhaseOutcome | None:
        view = self.load_view(session_id)
        bview = self._budget_view(session_id)
        step = self._steps[phase]
        try:
            outcome = await step(view, bview)
        except ArtifactError as exc:  # corrupted persisted artifact
            logger.error("artifact error at %s: %s", phase.value, exc)
            self.store.transition(
                session_id, PhaseName.FAILED.value, stop_reason=f"artifact-error: {exc}"
            )
            return None
        except Exception as exc:  # noqa: BLE001 — degrade, never lose the session
            logger.exception("phase %s failed", phase.value)
            self.store.transition(
                session_id,
                PhaseName.FAILED.value,
                stop_reason=f"{phase.value}: {type(exc).__name__}: {exc}",
            )
            return None
        self.store.transition(
            session_id,
            outcome.next_phase.value,
            artifacts=outcome.artifacts,
            bumps=outcome.bumps,
            stop_reason=(
                outcome.stop_reason if outcome.stop_reason is not None else False
            ),
        )
        return outcome

    def _force_advance(self, session_id: str, phase: PhaseName, reason: str) -> None:
        """Budget violated: push forward with whatever evidence exists (01 §3).
        Failing loudly with no output is worse than a hedged answer."""
        nxt = {
            PhaseName.PLAN: PhaseName.CLARIFY,
            PhaseName.COLLECT: PhaseName.REFLECT,
            PhaseName.REFLECT: PhaseName.CLARIFY,
            PhaseName.CLARIFY: PhaseName.DEEPEN,
            PhaseName.DEEPEN: PhaseName.RECOMMEND,
            PhaseName.RECOMMEND: PhaseName.DONE,
        }[phase]
        logger.warning(
            "budget (%s) violated at %s -> %s", reason, phase.value, nxt.value
        )
        if (
            nxt is PhaseName.DONE
            and self.store.latest_artifact(session_id, "RecommendationResult") is None
        ):
            # cannot "finish" without a result: go through RECOMMEND degraded
            nxt = PhaseName.RECOMMEND
        self.store.transition(session_id, nxt.value, stop_reason=reason)

    def _budget_allows(self, session_id: str, phase: PhaseName) -> bool:
        used = self.store.budget_used
        if phase in (PhaseName.COLLECT,):
            return used(session_id, "collect_calls") < self.budget.max_collect_calls
        if phase is PhaseName.REFLECT:
            return used(session_id, "research_iters") < self.budget.max_research_iters
        if phase is PhaseName.DEEPEN:
            return used(session_id, "deepen_calls") < self.budget.max_deepen_calls
        if phase is PhaseName.CLARIFY:
            return used(session_id, "clarify_rounds") < self.budget.max_clarify_rounds
        return True

    def _init_counters(self, session_id: str) -> None:
        caps = self.budget.caps()
        for name, cap in caps.items():
            self.store.set_budget(session_id, name, cap)

    def _budget_view(self, session_id: str) -> BudgetView:
        used = {
            n: self.store.budget_used(session_id, n)
            for n in (
                "collect_calls",
                "research_iters",
                "deepen_calls",
                "clarify_rounds",
            )
        }
        caps = self.budget.caps()
        return BudgetView(remaining={n: max(0, caps[n] - used[n]) for n in caps})

    def load_view(self, session_id: str) -> SessionView:
        kinds = (
            "ResearchBrief",
            "ResearchPlan",
            "EvidencePack",
            "Clarification",
            "UserAnswers",
            "PreferenceProfile",
            "RecommendationResult",
            "ReflectDecision",
            "BatchReport",
        )
        artifacts = {k: self.store.artifacts(session_id, k) for k in kinds}
        findings = self.store.findings(session_id)
        capture_sources = {
            c.id: (c.url, c.title) for c in self.store.captures(session_id)
        }
        return SessionView(
            session_id,
            self.store.get_phase(session_id),
            artifacts,
            findings,
            capture_sources,
            stop_reason=self.store.get_stop_reason(session_id),
        )
