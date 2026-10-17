"""Async task queue with lane-based isolation.

Two layers, separate concerns:
- TaskQueue (this package): schedules tasks into isolated lanes with
  configurable worker pools and backpressure. It is model-agnostic — workers
  call ``client.chat()`` and the token bucket in llm_client rate-limits.
- TokenBucket (in llm_client): rate-limits calls to LLM providers.

The supported surface is constructing ``TaskQueue`` directly and owning its
lifecycle. Prefer the async context manager, which starts on enter and
drains+stops on exit (declare lanes *before* entering — ``lane()`` refuses to
run after start)::

    from task_queue import TaskQueue

    q = TaskQueue()
    q.lane("work", workers=4)
    async with q:
        result = await q.submit("work", my_fn, arg1)

There is deliberately no process-wide default queue and no ``@queued``
decorator: an implicit singleton has no owner to call ``stop()``, so it either
crashes on submit (never started) or leaks workers (auto-started, never
drained). See docs/queue-service-design.md §12 (adoption ruling A-2 / plan
§12.2).
"""

from .service import LaneStats, QueueClosed, TaskQueue

__all__ = [
    "LaneStats",
    "QueueClosed",
    "TaskQueue",
]
