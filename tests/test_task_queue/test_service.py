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
    await q.stop()


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


async def test_retry_does_not_leak_join_accounting():
    """Every get() must be matched by a task_done(), even on the retry path.

    Regression: the retry branch re-put the task without calling task_done(),
    so the unfinished-task count leaked +1 per retry and queue.join() hung
    forever — any future join-based drain would deadlock.
    """
    q = TaskQueue()
    q.lane("work", workers=1)
    await q.start()

    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError(f"transient #{attempts}")
        return "ok"

    future = await q.submit("work", flaky, retries=3)
    assert await future == "ok"
    assert attempts == 3

    # join() must return promptly: no leaked unfinished-task accounting.
    await asyncio.wait_for(q._lanes["work"].queue.join(), timeout=2.0)

    await q.stop()


async def test_async_context_manager_starts_and_stops():
    """`async with` is the supported lifecycle: start on enter, drain+stop on
    exit, so a consumer can't forget to shut the workers down. Lanes are
    declared before entering (lane() refuses to run after start)."""
    q = TaskQueue()
    q.lane("work", workers=2)

    async def add(a, b):
        return a + b

    async with q as entered:
        assert entered is q
        future = await q.submit("work", add, 3, 4)
        assert await future == 7

    # exited the block -> stopped -> no longer accepting.
    with pytest.raises(QueueClosed):
        await q.submit("work", add, 1, 2)


async def test_async_context_manager_stops_on_exception():
    q = TaskQueue()
    q.lane("work", workers=1)

    with pytest.raises(RuntimeError, match="boom"):
        async with q:
            raise RuntimeError("boom")

    # Workers were still shut down despite the exception propagating.
    with pytest.raises(QueueClosed):
        await q.submit("work", lambda: None)


async def _force_cleanup_queue(q):
    q._accepting = False
    workers = [w for lane in q._lanes.values() for w in lane.workers]
    pending = list(getattr(q, "_submissions", ()))
    shutdown = getattr(q, "_stop_task", None)
    if shutdown is not None:
        pending.append(shutdown)
    for task in workers + pending:
        task.cancel()
    await asyncio.gather(*workers, *pending, return_exceptions=True)
    for lane in q._lanes.values():
        while not lane.queue.empty():
            item = lane.queue.get_nowait()
            if hasattr(item, "future") and not item.future.done():
                item.future.cancel()
            lane.queue.task_done()


async def test_retry_with_full_queue_finishes_and_balances_join(monkeypatch):
    import task_queue.service as service

    monkeypatch.setattr(service, "_RETRY_BACKOFF_BASE", 0)
    q = TaskQueue()
    q.lane("work", workers=1, maxsize=1)
    await q.start()
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            raise ValueError("transient")
        return "recovered"

    async def second():
        return "second"

    try:
        first = await q.submit("work", flaky, retries=1)
        await entered.wait()
        following = await q.submit("work", second)
        assert q._lanes["work"].queue.full()
        release.set()
        assert await asyncio.wait_for(asyncio.shield(first), 1) == "recovered"
        assert await asyncio.wait_for(following, 1) == "second"
        await asyncio.wait_for(q._lanes["work"].queue.join(), 1)
        assert attempts == 2
        assert q.stats()["work"].completed == 2
    finally:
        await _force_cleanup_queue(q)


async def test_stop_deadline_includes_full_queue_and_settles_futures():
    q = TaskQueue()
    q.lane("work", workers=1, maxsize=1)
    await q.start()
    entered = asyncio.Event()
    never = asyncio.Event()

    async def stuck():
        entered.set()
        await never.wait()

    try:
        running = await q.submit("work", stuck)
        await entered.wait()
        queued = await q.submit("work", stuck)
        await asyncio.wait_for(q.stop(timeout=0.05), 1)
        assert running.cancelled()
        assert queued.cancelled()
        await asyncio.wait_for(q._lanes["work"].queue.join(), 1)
        stats = q.stats()["work"]
        assert stats.running == stats.pending == 0
        await q.stop(timeout=0.05)
    finally:
        await _force_cleanup_queue(q)


async def test_stop_unblocks_pending_submit_and_allows_clean_restart():
    q = TaskQueue()
    q.lane("work", workers=1, maxsize=1)
    await q.start()
    entered, submitting, never = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stuck():
        entered.set()
        await never.wait()

    async def submit_one():
        submitting.set()
        return await q.submit("work", stuck)

    async def healthy():
        return 7

    blocked = None
    try:
        first = await q.submit("work", stuck)
        await entered.wait()
        second = await q.submit("work", stuck)
        blocked = asyncio.create_task(submit_one())
        await submitting.wait()
        await asyncio.wait_for(q.stop(timeout=0.05), 1)
        with pytest.raises(QueueClosed):
            await asyncio.wait_for(blocked, 1)
        assert first.cancelled() and second.cancelled()
        assert not q._submissions
        await q.start()
        result = await q.submit("work", healthy)
        assert await asyncio.wait_for(result, 1) == 7
        assert len(q._lanes["work"].workers) == 1
        await q.stop()
    finally:
        if blocked is not None and not blocked.done():
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
        await _force_cleanup_queue(q)


async def test_cancelling_stop_caller_does_not_cancel_cleanup():
    q = TaskQueue()
    q.lane("work", workers=1, maxsize=1)
    await q.start()
    entered, stopping, never = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stuck():
        entered.set()
        await never.wait()

    async def stop_caller():
        stopping.set()
        await q.stop(timeout=0.05)

    try:
        result = await q.submit("work", stuck)
        await entered.wait()
        caller = asyncio.create_task(stop_caller())
        await stopping.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.wait_for(q.stop(timeout=0.05), 1)
        assert result.cancelled()
        assert not q._started
        assert not q._lanes["work"].workers
    finally:
        await _force_cleanup_queue(q)


async def test_task_cancelled_error_does_not_kill_worker():
    q = TaskQueue()
    q.lane("work", workers=1)

    async def cancelled():
        raise asyncio.CancelledError

    async def healthy():
        return "ok"

    async with q:
        result = await q.submit("work", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await result
        next_result = await q.submit("work", healthy)
        assert await asyncio.wait_for(next_result, 1) == "ok"
        await asyncio.wait_for(q._lanes["work"].queue.join(), 1)


async def test_start_rejected_while_shutdown_in_flight():
    """start() during an in-flight shutdown is explicitly rejected; the
    shared cleanup task keeps running."""
    q = TaskQueue()
    q.lane("work", workers=1, maxsize=1)
    await q.start()
    entered, never = asyncio.Event(), asyncio.Event()

    async def stuck():
        entered.set()
        await never.wait()

    try:
        result = await q.submit("work", stuck)
        await entered.wait()
        stopping = asyncio.create_task(q.stop(timeout=0.05))
        await asyncio.sleep(0)  # let stop() seal submissions and spawn cleanup
        with pytest.raises(RuntimeError, match="stopping"):
            await q.start()
        await asyncio.wait_for(stopping, 1)
        assert result.cancelled()
        assert not q._started
    finally:
        await _force_cleanup_queue(q)
