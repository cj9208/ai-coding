"""Non-invasive decorator for submitting async functions to a TaskQueue.

Usage::

    from task_queue import queued, get_queue

    @queued(lane="enrich", retries=3)
    async def enrich_one(chunk):
        return await client.chat_json(...)

    # await for result (same call shape as direct invocation)
    result = await enrich_one(chunk)

    # fire-and-forget (schedule many, collect later)
    futures = [enrich_one.submit(chunk) for chunk in chunks]
    results = await asyncio.gather(*futures)
"""

from __future__ import annotations

import asyncio
import functools
import os
from typing import Any, Awaitable, Callable, TypeVar

from .service import TaskQueue

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

_default_queue: TaskQueue | None = None


def get_queue() -> TaskQueue:
    """Process-wide default TaskQueue. Created lazily on first call.

    Configuration via environment variables:
    - QUEUE_DEFAULT_LANES: comma-separated ``name:workers:maxsize`` triples
      (default: ``default:4:1000``)

    Callers that need custom lane declarations should create their own
    TaskQueue instead of relying on this singleton.
    """
    global _default_queue
    if _default_queue is None:
        _default_queue = _build_default_queue()
    return _default_queue


def _build_default_queue() -> TaskQueue:
    q = TaskQueue()
    raw = os.environ.get("QUEUE_DEFAULT_LANES", "default:4:1000")
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        name = parts[0]
        workers = int(parts[1]) if len(parts) > 1 else 4
        maxsize = int(parts[2]) if len(parts) > 2 else 1000
        q.lane(name, workers=workers, maxsize=maxsize)
    return q


def set_default_queue(q: TaskQueue) -> None:
    """Replace the process-wide default queue (mainly for tests)."""
    global _default_queue
    _default_queue = q


def queued(
    fn: F | None = None,
    *,
    lane: str = "default",
    retries: int = 3,
    queue: TaskQueue | None = None,
) -> Any:
    """Decorator: route an async function through a TaskQueue lane.

    The decorated function keeps its name and signature. Calling it
    returns an awaitable (same shape as the original async function).
    An extra ``.submit()`` async method is attached for fire-and-forget
    usage — it returns a Future for the result without awaiting it.
    """

    def decorator(func: F) -> Any:
        target_queue = queue

        def _resolve_queue() -> TaskQueue:
            return target_queue or get_queue()

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            q = _resolve_queue()
            future = await q.submit(lane, func, *args, retries=retries, **kwargs)
            return await future

        async def submit(*args: Any, **kwargs: Any) -> asyncio.Future[Any]:
            """Submit and return a Future for the result (don't await it)."""
            q = _resolve_queue()
            return await q.submit(lane, func, *args, retries=retries, **kwargs)

        wrapper.submit = submit  # type: ignore[attr-defined]
        wrapper._original = func  # type: ignore[attr-defined]
        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator
