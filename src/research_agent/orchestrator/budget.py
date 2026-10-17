"""Budgets: policy lives in code, judgment lives in prompts (01 §1.2).

The orchestrator owns the counters; phases receive a BudgetView (read-only
remaining amounts) so they can plan within it, and report what they consumed
in the PhaseResult — the bump is committed in the same transaction as the
phase's artifacts (05 §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Budget:
    max_collect_calls: int = 30  # search+fetch calls by collectors
    max_research_iters: int = 3  # COLLECT->REFLECT loops in phase 1
    max_deepen_calls: int = 8  # second, targeted collection
    max_questions: int = 4  # per clarification round
    max_clarify_rounds: int = 1  # v1 default; 2nd round is a v1+ feature
    wallclock_seconds: int = 600

    def caps(self) -> dict[str, int]:
        return {
            "collect_calls": self.max_collect_calls,
            "research_iters": self.max_research_iters,
            "deepen_calls": self.max_deepen_calls,
            "clarify_rounds": self.max_clarify_rounds,
        }


@dataclass
class BudgetView:
    """Remaining budget as seen by one phase run. Names match counters."""

    remaining: dict[str, int] = field(default_factory=dict)

    def left(self, name: str) -> int:
        return self.remaining.get(name, 0)

    @property
    def collect_left(self) -> int:
        return self.left("collect_calls")

    @property
    def deepen_left(self) -> int:
        return self.left("deepen_calls")

    @property
    def iters_left(self) -> int:
        return self.left("research_iters")


class CallBudget:
    """Concurrency-safe cap on the total billable calls inside ONE collector
    step (queue-service-design.md §12.4, A-3 breach #1).

    The serial collector got atomicity for free: its ``used >= cap`` check and
    the ``used += 1`` deduction straddle an ``await``, but nothing else ran in
    between, so check-then-act was safe. Fanned out onto a lane, N workers all
    read "budget left" before any deducts and blow past the cap. ``reserve()``
    collapses check-and-deduct into one call with no ``await`` inside, so it is
    atomic under asyncio — the property concurrency would otherwise lose.

    In-memory and step-scoped on purpose: the authoritative counter is still
    the DB row, committed with the step's artifacts (machine 05 §4). This only
    guards the concurrent window *within* a step, so there is no cross-step
    race — the next step re-derives ``calls_left`` from the committed total.
    """

    def __init__(self, total: int) -> None:
        self._remaining = max(0, total)
        self._used = 0

    def reserve(self, n: int = 1) -> bool:
        """Atomically claim *n* calls. False (nothing claimed) if fewer than
        *n* remain. Safe to call from concurrent workers: no await inside."""
        if self._remaining < n:
            return False
        self._remaining -= n
        self._used += n
        return True

    def release(self, n: int = 1) -> None:
        """Give back unused reservation (coarse per-query reservation would
        need this; the fine-grained per-call path does not)."""
        n = min(n, self._used)
        self._remaining += n
        self._used -= n

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def used(self) -> int:
        return self._used


COUNTER_FOR_PHASE_STEP = {
    "collect": "collect_calls",
    "deepen": "deepen_calls",
    "reflect_loop": "research_iters",
    "clarify": "clarify_rounds",
}
