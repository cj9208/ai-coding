"""Tests for queue.decorators — @queued, get_queue, submit API."""

import asyncio

from task_queue import TaskQueue, get_queue, queued, set_default_queue


def _make_queue() -> TaskQueue:
    q = TaskQueue()
    q.lane("test", workers=2, maxsize=100)
    return q


async def test_queued_decorator_basic():
    q = _make_queue()
    await q.start()

    @queued(lane="test", queue=q)
    async def add(a, b):
        return a + b

    result = await add(3, 4)
    assert result == 7

    await q.stop()


async def test_queued_preserves_function_name():
    q = _make_queue()

    @queued(lane="test", queue=q)
    async def my_function():
        return 42

    assert my_function.__name__ == "my_function"


async def test_queued_submit_returns_future():
    q = _make_queue()
    await q.start()

    @queued(lane="test", queue=q)
    async def compute(x):
        await asyncio.sleep(0.01)
        return x * 2

    future = await compute.submit(5)
    assert isinstance(future, asyncio.Future)
    result = await future
    assert result == 10

    await q.stop()


async def test_queued_gather_pattern():
    q = _make_queue()
    await q.start()

    @queued(lane="test", queue=q)
    async def double(x):
        return x * 2

    futures = [await double.submit(i) for i in range(5)]
    results = await asyncio.gather(*futures)
    assert sorted(results) == [0, 2, 4, 6, 8]

    await q.stop()


async def test_queued_with_retries():
    q = _make_queue()
    await q.start()

    attempts = 0

    @queued(lane="test", retries=2, queue=q)
    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError("not yet")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert attempts == 3

    await q.stop()


async def test_get_queue_creates_singleton():
    set_default_queue(None)  # reset
    q1 = get_queue()
    q2 = get_queue()
    assert q1 is q2
    set_default_queue(None)  # cleanup


async def test_get_queue_respects_env_var(monkeypatch):
    set_default_queue(None)
    monkeypatch.setenv("QUEUE_DEFAULT_LANES", "alpha:2:50,beta:3:100")
    q = get_queue()
    stats = q.stats()
    assert "alpha" in stats
    assert "beta" in stats
    set_default_queue(None)


async def test_queued_without_explicit_queue_uses_default():
    set_default_queue(None)
    q = _make_queue()
    set_default_queue(q)
    await q.start()

    @queued(lane="test")
    async def compute():
        return 99

    result = await compute()
    assert result == 99

    await q.stop()
    set_default_queue(None)


async def test_queued_original_accessible():
    q = _make_queue()

    async def original_fn():
        return 42

    decorated = queued(lane="test", queue=q)(original_fn)
    assert decorated._original is original_fn
