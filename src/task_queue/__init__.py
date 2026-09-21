"""Async task queue with lane-based isolation and non-invasive decorator.

Two layers, separate concerns:
- TaskQueue (this package): schedules tasks into isolated lanes with
  configurable worker pools and backpressure.
- TokenBucket (in llm_client): rate-limits calls to LLM providers.

Quick start::

    from task_queue import TaskQueue, queued

    # Option A: explicit queue
    q = TaskQueue()
    q.lane("work", workers=4)
    await q.start()
    result = await q.submit("work", my_fn, arg1)
    await q.stop()

    # Option B: decorator with process-wide default queue
    @queued(lane="default")
    async def my_fn(arg):
        ...

    await my_fn(arg)  # transparently routed through the queue
"""

from .decorators import get_queue, queued, set_default_queue
from .service import LaneStats, QueueClosed, TaskQueue

__all__ = [
    "LaneStats",
    "QueueClosed",
    "TaskQueue",
    "get_queue",
    "queued",
    "set_default_queue",
]
