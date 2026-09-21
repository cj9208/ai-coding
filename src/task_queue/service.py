"""Async task queue with lane-based isolation.

Each lane has its own queue and worker pool, so different task types
(e.g. batch enrich vs. latency-sensitive query) don't starve each other.
Backpressure propagates naturally: when a lane's queue is full, submit()
blocks; when workers are blocked (e.g. by LLM rate limiting), they stop
pulling from the queue, which eventually fills up.

See docs/queue-service-design.md for the full architecture.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_SHUTDOWN = object()

TaskFn = Callable[..., Awaitable[Any]]


@dataclass
class LaneStats:
    """Snapshot of a lane's activity."""

    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0


@dataclass
class _Task:
    fn: TaskFn
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: asyncio.Future[Any]
    retries: int = 0
    max_retries: int = 3


class _Lane:
    """Internal: one isolated queue + worker pool."""

    def __init__(self, name: str, worker_count: int, maxsize: int) -> None:
        self.name = name
        self.worker_count = worker_count
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.workers: list[asyncio.Task[None]] = []
        self.stats = LaneStats()

    @property
    def running(self) -> int:
        return self.stats.running

    @running.setter
    def running(self, value: int) -> None:
        self.stats.running = value


class QueueClosed(RuntimeError):
    """Raised when submitting to a stopped queue."""


class TaskQueue:
    """Async task queue with lane-based isolation.

    Usage::

        q = TaskQueue()
        q.lane("enrich", workers=4, maxsize=500)
        q.lane("query", workers=6, maxsize=100)
        await q.start()

        future = await q.submit("enrich", my_fn, arg1, arg2)
        result = await future

        await q.stop()
    """

    def __init__(self) -> None:
        self._lanes: dict[str, _Lane] = {}
        self._accepting = False
        self._started = False

    def lane(
        self,
        name: str,
        *,
        workers: int = 4,
        maxsize: int = 1000,
    ) -> None:
        """Declare a lane. Must be called before start()."""
        if self._started:
            raise RuntimeError(f"Cannot add lane {name!r} after start()")
        if name in self._lanes:
            raise RuntimeError(f"Lane {name!r} already declared")
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._lanes[name] = _Lane(name, workers, maxsize)

    async def start(self) -> None:
        """Start all lane worker pools."""
        if self._started:
            return
        self._accepting = True
        self._started = True
        for lane_obj in self._lanes.values():
            for i in range(lane_obj.worker_count):
                t = asyncio.create_task(
                    self._worker_loop(lane_obj), name=f"queue-{lane_obj.name}-{i}"
                )
                lane_obj.workers.append(t)
        logger.info(
            "TaskQueue started: %s",
            {name: l.worker_count for name, l in self._lanes.items()},
        )

    async def stop(self, *, timeout: float = 30) -> None:
        """Graceful shutdown: drain queued tasks, then stop workers."""
        if not self._started:
            return
        self._accepting = False

        for lane_obj in self._lanes.values():
            for _ in lane_obj.workers:
                await lane_obj.queue.put(_SHUTDOWN)

        all_workers = [w for lane in self._lanes.values() for w in lane.workers]
        if all_workers:
            _, pending = await asyncio.wait(all_workers, timeout=timeout)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning(
                    "TaskQueue stop: cancelled %d workers after %.0fs timeout",
                    len(pending),
                    timeout,
                )

        self._started = False
        logger.info("TaskQueue stopped")

    async def submit(
        self,
        lane: str,
        fn: TaskFn,
        *args: Any,
        retries: int = 3,
        **kwargs: Any,
    ) -> asyncio.Future[Any]:
        """Submit a task to a lane. Blocks if the lane's queue is full.

        Returns a Future that resolves to the task's return value.
        Raises QueueClosed if the queue is shutting down.
        Raises KeyError if the lane doesn't exist.
        """
        if not self._accepting:
            raise QueueClosed("Queue is not accepting tasks")
        lane_obj = self._lanes.get(lane)
        if lane_obj is None:
            raise KeyError(f"Unknown lane: {lane!r}")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        task = _Task(
            fn=fn, args=args, kwargs=kwargs, future=future, max_retries=retries
        )
        await lane_obj.queue.put(task)
        lane_obj.stats.pending = lane_obj.queue.qsize()
        return future

    def stats(self) -> dict[str, LaneStats]:
        """Snapshot of all lane stats."""
        result = {}
        for name, lane_obj in self._lanes.items():
            lane_obj.stats.pending = lane_obj.queue.qsize()
            result[name] = LaneStats(
                pending=lane_obj.stats.pending,
                running=lane_obj.stats.running,
                completed=lane_obj.stats.completed,
                failed=lane_obj.stats.failed,
            )
        return result

    async def _worker_loop(self, lane_obj: _Lane) -> None:
        while True:
            item = await lane_obj.queue.get()
            if item is _SHUTDOWN:
                lane_obj.queue.task_done()
                break

            task: _Task = item
            lane_obj.running += 1
            lane_obj.stats.pending = lane_obj.queue.qsize()
            try:
                result = await task.fn(*task.args, **task.kwargs)
                if not task.future.done():
                    task.future.set_result(result)
                lane_obj.stats.completed += 1
                lane_obj.queue.task_done()
            except Exception as exc:
                if task.retries < task.max_retries:
                    task.retries += 1
                    await lane_obj.queue.put(task)
                    lane_obj.stats.pending = lane_obj.queue.qsize()
                    logger.debug(
                        "Task in lane %r failed (retry %d/%d): %s",
                        lane_obj.name,
                        task.retries,
                        task.max_retries,
                        exc,
                    )
                else:
                    if not task.future.done():
                        task.future.set_exception(exc)
                    lane_obj.stats.failed += 1
                    lane_obj.queue.task_done()
                    logger.warning(
                        "Task in lane %r failed after %d retries: %s",
                        lane_obj.name,
                        task.max_retries,
                        exc,
                    )
            finally:
                lane_obj.running -= 1
