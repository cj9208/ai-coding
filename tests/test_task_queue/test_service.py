"""Tests for queue.service — TaskQueue, lane isolation, retry, shutdown."""

import asyncio

import pytest

from task_queue.service import QueueClosed, TaskQueue


async def test_basic_submit_and_result():
    q = TaskQueue()
    q.lane("work", workers=2)
    await q.start()

    async def add(a, b):
        return a + b

    future = await q.submit("work", add, 3, 4)
    result = await future
    assert result == 7

    await q.stop()


async def test_multiple_tasks_run_concurrently():
    q = TaskQueue()
    q.lane("work", workers=4)
    await q.start()

    running = 0
    peak = 0

    async def track():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return "done"

    futures = [await q.submit("work", track) for _ in range(8)]
    results = await asyncio.gather(*futures)
    assert all(r == "done" for r in results)
    assert peak > 1  # concurrency actually happened


async def test_lane_isolation():
    """Tasks in different lanes don't block each other."""
    q = TaskQueue()
    q.lane("fast", workers=2)
    q.lane("slow", workers=1)
    await q.start()

    async def fast_task():
        return "fast"

    async def slow_task():
        await asyncio.sleep(0.2)
        return "slow"

    slow_future = await q.submit("slow", slow_task)
    await asyncio.sleep(0.01)  # let slow task start
    fast_future = await q.submit("fast", fast_task)

    fast_result = await fast_future
    assert fast_result == "fast"

    slow_result = await slow_future
    assert slow_result == "slow"

    await q.stop()


async def test_retry_on_failure():
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()

    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError(f"fail #{attempts}")
        return "ok"

    future = await q.submit("work", flaky, retries=3)
    result = await future
    assert result == "ok"
    assert attempts == 3

    await q.stop()


async def test_exhausted_retries_set_exception():
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()

    async def always_fails():
        raise RuntimeError("boom")

    future = await q.submit("work", always_fails, retries=2)
    with pytest.raises(RuntimeError, match="boom"):
        await future

    stats = q.stats()
    assert stats["work"].failed == 1

    await q.stop()


async def test_stats_tracking():
    q = TaskQueue()
    q.lane("work", workers=2)
    await q.start()

    async def noop():
        await asyncio.sleep(0.01)
        return None

    futures = [await q.submit("work", noop) for _ in range(5)]
    await asyncio.gather(*futures)

    stats = q.stats()
    assert stats["work"].completed == 5
    assert stats["work"].failed == 0
    assert stats["work"].pending == 0

    await q.stop()


async def test_submit_to_unknown_lane_raises():
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()

    with pytest.raises(KeyError, match="nope"):
        await q.submit("nope", lambda: None)

    await q.stop()


async def test_submit_after_stop_raises():
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()
    await q.stop()

    with pytest.raises(QueueClosed):
        await q.submit("work", lambda: None)


async def test_duplicate_lane_raises():
    q = TaskQueue()
    q.lane("work", workers=1)
    with pytest.raises(RuntimeError, match="already declared"):
        q.lane("work", workers=2)


async def test_add_lane_after_start_raises():
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()
    with pytest.raises(RuntimeError, match="after start"):
        q.lane("new", workers=1)
    await q.stop()


async def test_graceful_stop_drains_queue():
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()

    results = []

    async def record(x):
        await asyncio.sleep(0.01)
        results.append(x)
        return x

    futures = [await q.submit("work", record, i) for i in range(5)]
    await q.stop()

    assert sorted(results) == [0, 1, 2, 3, 4]
    assert all(f.done() for f in futures)


async def test_backpressure_on_full_queue():
    q = TaskQueue()
    q.lane("work", workers=1, maxsize=2)
    await q.start()

    barrier = asyncio.Event()

    async def blocker():
        await barrier.wait()
        return "done"

    # Fill the worker + queue (1 running + 2 queued = 3 total)
    await q.submit("work", blocker)
    await asyncio.sleep(0.01)  # let worker pick it up
    await q.submit("work", blocker)
    await q.submit("work", blocker)

    # Next submit should block (queue is full)
    submitted = False

    async def try_submit():
        nonlocal submitted
        await q.submit("work", blocker)
        submitted = True

    submit_task = asyncio.create_task(try_submit())
    await asyncio.sleep(0.05)
    assert not submitted  # still blocked

    barrier.set()  # unblock all tasks
    await submit_task
    assert submitted

    await q.stop()


async def test_zero_workers_rejected():
    q = TaskQueue()
    with pytest.raises(ValueError, match="workers must be >= 1"):
        q.lane("bad", workers=0)
