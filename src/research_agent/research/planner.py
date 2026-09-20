"""Planner (02 §2): query decomposition — the highest-leverage prompt, so it
gets its own validation pass."""

from __future__ import annotations

import logging

from ..contracts.models import ResearchBrief, ResearchPlan
from ..contracts.prompts import render_prompt

logger = logging.getLogger(__name__)

VALID_INTENTS: set[str] = {"survey", "comparison", "constraint", "recency", "risk"}
MIN_INTENTS = 3  # the prompt says so; code enforces it once


class Planner:
    def __init__(self, llm):
        self.llm = llm

    async def make_plan(
        self,
        brief: ResearchBrief,
        remaining_calls: int,
        *,
        max_queries: int = 6,
        deepen_hint: str = "",
    ) -> ResearchPlan:
        prompt = render_prompt(
            "planner",
            TOPIC=brief.topic,
            USER_CONTEXT=brief.user_context or "(none)",
            DOMAIN_HINT=brief.candidate_domain_hint or "(none)",
            MAX_QUERIES=str(max_queries),
            REMAINING_CALLS=str(remaining_calls),
        )
        if deepen_hint:
            prompt += (
                "\n\nMODE: targeted follow-up (DEEPEN). The user just "
                "answered clarifying questions; produce ONLY priority=1 "
                "sub-queries that the answers made relevant.\n" + deepen_hint
            )

        plan = await self.llm.chat_json(prompt, schema=ResearchPlan, temperature=0.4)
        problems = self._validate(plan, max_queries)
        if problems:  # one correction turn, then accept with a warning
            fix = (
                "Your plan had problems: "
                + "; ".join(problems)
                + "\nOutput the corrected full JSON plan only."
            )
            try:
                repaired = await self.llm.chat_json(
                    fix + "\n\n" + prompt, schema=ResearchPlan, temperature=0.2
                )
                if not self._validate(repaired, max_queries):
                    plan = repaired
                else:
                    logger.warning(
                        "planner correction still invalid: %s; keeping v1",
                        self._validate(repaired, max_queries),
                    )
            except Exception:  # noqa: BLE001 — a mediocre plan beats a dead session
                logger.warning("planner repair failed; keeping first plan")
        # code owns identity: LLM ids are advisory
        for i, sq in enumerate(plan.sub_queries, 1):
            sq.id = sq.id or f"q{i}"
        return plan

    @staticmethod
    def _validate(plan: ResearchPlan, max_queries: int) -> list[str]:
        problems = []
        intents: set[str] = {sq.intent for sq in plan.sub_queries}
        if len(plan.sub_queries) == 0:
            problems.append("no sub-queries")
        if len(plan.sub_queries) > max_queries:
            problems.append(f"more than {max_queries} sub-queries")
        if intents - VALID_INTENTS:
            problems.append(f"unknown intents {intents - VALID_INTENTS}")
        if len(intents) < MIN_INTENTS and len(plan.sub_queries) >= MIN_INTENTS:
            problems.append(
                f"only {len(intents)} distinct intents; need >= {MIN_INTENTS} "
                "for a recommendation-shaped topic"
            )
        return problems
