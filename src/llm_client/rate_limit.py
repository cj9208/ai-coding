"""Async token bucket rate limiter for LLM API calls.

Each model gets its own bucket, configured in requests-per-minute (RPM).
The bucket refills at a steady rate; acquire() blocks when empty,
propagating backpressure to the caller (typically a TaskQueue worker).

See docs/queue-service-design.md §4 for the architecture.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TokenBucket:
    """Async token bucket. Refills one token every (60/rpm) seconds.

    Usage::

        bucket = TokenBucket(rpm=60)
        await bucket.start()      # start the refill background task

        await bucket.acquire()    # blocks until a token is available
        # ... make the API call ...

        await bucket.stop()       # stop the refill task
    """

    def __init__(self, rpm: int) -> None:
        if rpm < 1:
            raise ValueError("rpm must be >= 1")
        self.rpm = rpm
        self._semaphore = asyncio.Semaphore(0)
        self._refiller: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        """Start the background refill task."""
        if self._started:
            return
        self._started = True
        interval = 60.0 / self.rpm
        self._refiller = asyncio.create_task(
            self._refill_loop(interval), name=f"token-bucket-{self.rpm}rpm"
        )

    async def stop(self) -> None:
        """Stop the background refill task."""
        if self._refiller is not None:
            self._refiller.cancel()
            try:
                await self._refiller
            except asyncio.CancelledError:
                pass
            self._refiller = None
        self._started = False

    async def _refill_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._semaphore.release()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        await self._semaphore.acquire()


class RateLimiter:
    """Per-model rate limiter. Manages a collection of TokenBuckets.

    Usage::

        limiter = RateLimiter({"deepseek-chat": 60, "text-embedding-3-small": 120})
        await limiter.start()

        await limiter.acquire("deepseek-chat")   # blocks until token available
        # ... make the API call ...

        await limiter.stop()
    """

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = limits or {}
        self._buckets: dict[str, TokenBucket] = {}

    async def start(self) -> None:
        """Start all configured buckets."""
        for model, rpm in self._limits.items():
            bucket = TokenBucket(rpm)
            await bucket.start()
            self._buckets[model] = bucket
        if self._limits:
            logger.info("RateLimiter started: %s", self._limits)

    async def stop(self) -> None:
        """Stop all buckets."""
        for bucket in self._buckets.values():
            await bucket.stop()
        self._buckets.clear()

    async def acquire(self, model: str) -> None:
        """Wait for a token for the given model. No-op if not configured."""
        bucket = self._buckets.get(model)
        if bucket is not None:
            await bucket.acquire()

    @property
    def configured_models(self) -> set[str]:
        return set(self._limits.keys())
