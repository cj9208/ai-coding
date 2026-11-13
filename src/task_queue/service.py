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

# Exponential backoff bounds for retrying a task in its current worker.
_RETRY_BACKOFF_BASE = 0.1
_RETRY_BACKOFF_CAP = 5.0

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
        self._submissions: set[asyncio.Task[None]] = set()
        self._stop_task: asyncio.Task[None] | None = None

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
        if self._stop_task is not None and not self._stop_task.done():
            raise RuntimeError("Queue is stopping")
        if self._started:
            return
        self._stop_task = None
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
        if self._stop_task is None:
            if not self._started:
                return
            self._accepting = False
            self._stop_task = asyncio.create_task(
                self._shutdown(timeout), name="queue-shutdown"
            )
        await asyncio.shield(self._stop_task)

    async def _shutdown(self, timeout: float) -> None:
        putters = list(self._submissions)
        workers = [w for lane in self._lanes.values() for w in lane.workers]
        for putter in putters:
            if not putter.done():
                putter.cancel()
        try:
            async with asyncio.timeout(max(0.0, timeout)):
                await asyncio.gather(*putters, return_exceptions=True)
                await asyncio.gather(
                    *(lane.queue.join() for lane in self._lanes.values())
                )
        except TimeoutError:
            logger.warning("TaskQueue stop: drain exceeded %.3fs", timeout)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await asyncio.gather(*putters, return_exceptions=True)
            for lane in self._lanes.values():
                while True:
                    try:
                        task: _Task = lane.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if not task.future.done():
                        task.future.cancel()
                    lane.queue.task_done()
                lane.workers.clear()
                lane.stats.pending = 0
            self._started = False
            logger.info("TaskQueue stopped")

    async def __aenter__(self) -> "TaskQueue":
        """Start the queue. Declare lanes *before* entering — ``lane()``
        refuses to run after start. This is the one supported lifecycle:
        ``async with`` guarantees a drain+stop on the way out, so a consumer
        can't forget to shut the workers down."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        await self.stop()

    async def submit(
        self,
        lane: str,
        fn: TaskFn,
        *args: Any,
        retries: int = 3,
        **kwargs: Any,
    ) -> asyncio.Future[Any]:
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
        putter = asyncio.create_task(lane_obj.queue.put(task))
        self._submissions.add(putter)
        try:
            await putter
            if not self._accepting:
                future.cancel()
                raise QueueClosed("Queue closed during submission")
            lane_obj.stats.pending = lane_obj.queue.qsize()
            return future
        except asyncio.CancelledError:
            future.cancel()
            if not self._accepting:
                raise QueueClosed("Queue closed during submission") from None
            raise
        finally:
            self._submissions.discard(putter)

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
            task: _Task = item
            lane_obj.running += 1
            lane_obj.stats.pending = lane_obj.queue.qsize()
            try:
                while not task.future.done():
                    try:
                        result = await task.fn(*task.args, **task.kwargs)
                    except Exception as exc:
                        if task.future.done():
                            break
                        if task.retries >= task.max_retries:
                            task.future.set_exception(exc)
                            lane_obj.stats.failed += 1
                            logger.warning(
                                "Task in lane %r failed after %d retries: %s",
                                lane_obj.name,
                                task.max_retries,
                                exc,
                            )
                            break
                        task.retries += 1
                        await asyncio.sleep(
                            min(
                                _RETRY_BACKOFF_BASE * 2 ** (task.retries - 1),
                                _RETRY_BACKOFF_CAP,
                            )
                        )
                    else:
                        if not task.future.done():
                            task.future.set_result(result)
                            lane_obj.stats.completed += 1
                        break
            except asyncio.CancelledError:
                if not task.future.done():
                    task.future.cancel()
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                # task.fn 自己抛出的 CancelledError 只取消该任务，worker 继续。
            finally:
                lane_obj.running -= 1
                lane_obj.queue.task_done()
                lane_obj.stats.pending = lane_obj.queue.qsize()
