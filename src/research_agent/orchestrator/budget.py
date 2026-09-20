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


COUNTER_FOR_PHASE_STEP = {
    "collect": "collect_calls",
    "deepen": "deepen_calls",
    "reflect_loop": "research_iters",
    "clarify": "clarify_rounds",
}
