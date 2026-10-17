"""A-3 reserve-before-submit (queue-service-design.md §12.4).

Two layers of proof:
1. ``CallBudget`` never grants more than its cap, even when hammered from
   concurrent coroutines (the atomicity the serial loop used to get for free).
2. The collector's real per-query path, driven concurrently against one shared
   budget, stops exactly at ``calls_left`` — this is precisely what phase 3's
   lane will do, so the gate is tested now rather than assumed.
"""

from __future__ import annotations

import asyncio

from research_agent.contracts.models import ResearchBrief, SubQuery
from research_agent.orchestrator.budget import CallBudget
from research_agent.research.adapters.base import Fetched, Hit
from research_agent.research.collector import BatchOutcome, Collector
from research_agent.research.normalizer import SupportClusterer


def test_reserve_blocks_overrun():
    b = CallBudget(2)
    assert b.reserve()
    assert b.reserve()
    assert not b.reserve()
    assert b.used == 2
    assert b.remaining == 0


def test_reserve_multi_and_release():
    b = CallBudget(5)
    assert b.reserve(3)
    assert not b.reserve(3)  # only 2 left, refuses the whole claim
    assert b.remaining == 2 and b.used == 3
    b.release(1)
    assert b.remaining == 3 and b.used == 2


async def test_reserve_atomic_under_concurrency():
    """20 workers x 10 attempts = 200 claims on a cap of 100: exactly 100 are
    granted, never 101. An await between check and decrement would overrun."""
    b = CallBudget(100)

    async def worker() -> int:
        got = 0
        for _ in range(10):
            await asyncio.sleep(0)  # force interleaving between claims
            if b.reserve():
                got += 1
        return got

    granted = await asyncio.gather(*[worker() for _ in range(20)])
    assert sum(granted) == 100
    assert b.used == 100 and b.remaining == 0


class _SlowAdapter:
    """search/fetch yield to the event loop so concurrent workers really do
    interleave across their reserve() points."""

    kind = "web_search"

    async def search(self, q: str, k: int) -> list[Hit]:
        await asyncio.sleep(0)
        return [Hit(url=f"{q}-{i}", title="t", snippet="s") for i in range(k)]

    async def fetch(self, hit: Hit) -> Fetched:
        await asyncio.sleep(0)
        return Fetched(url=hit.url, text=f"body of {hit.url}", title="t")


class _EmptyLLM:
    async def chat_json(
        self, prompt, system_prompt="", *, schema=None, model=None, temperature=None
    ):
        await asyncio.sleep(0)
        return schema.model_validate({"findings": []})


class _FakeStore:
    """Only add_capture is reached (empty findings => no id allocation)."""

    def __init__(self) -> None:
        self.n = 0

    def add_capture(self, session_id, *, url, text, adapter, title=""):
        self.n += 1
        return f"C{self.n}", False


async def test_collector_concurrent_queries_never_exceed_cap():
    collector = Collector(_EmptyLLM(), {"web_search": _SlowAdapter()}, _FakeStore())
    brief = ResearchBrief(session_id="s", topic="t")
    cap = 5
    budget = CallBudget(cap)
    out = BatchOutcome()
    clusterer = SupportClusterer()
    queries = [
        SubQuery(id=f"q{i}", query=f"q{i}", adapter_hint="web_search")
        for i in range(10)  # 10 queries, each worth up to 7 calls, cap of 5
    ]

    await asyncio.gather(
        *[
            collector._run_one(brief, sq, clusterer, [], 1, budget, out)
            for sq in queries
        ]
    )

    # the invariant the old spend-then-deduct model would have breached
    assert budget.used <= cap
    assert budget.used == cap  # demand far exceeds cap, so it is fully spent
    assert out.findings == []
    assert out.failures  # the queries that lost the reserve race record why
