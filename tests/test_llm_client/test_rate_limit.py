"""Tests for llm_client.rate_limit — TokenBucket and RateLimiter."""

import time

import pytest

from llm_client.rate_limit import RateLimiter, TokenBucket


async def test_token_bucket_basic():
    bucket = TokenBucket(rpm=600)  # 10 per second
    await bucket.start()

    t0 = time.monotonic()
    for _ in range(3):
        await bucket.acquire()
    elapsed = time.monotonic() - t0

    # First token should be immediate (semaphore starts at 0, but refill
    # adds one every 0.1s). Three tokens at 10/s = ~0.2s wait.
    assert elapsed >= 0.15
    assert elapsed < 1.0

    await bucket.stop()


async def test_token_bucket_blocks_when_empty():
    bucket = TokenBucket(rpm=60)  # 1 per second
    await bucket.start()

    # First acquire should block for ~1s (bucket starts empty)
    t0 = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.8

    await bucket.stop()


async def test_token_bucket_invalid_rpm():
    with pytest.raises(ValueError, match="rpm must be >= 1"):
        TokenBucket(rpm=0)


async def test_rate_limiter_per_model():
    limiter = RateLimiter({"model-a": 600, "model-b": 600})
    await limiter.start()

    assert limiter.configured_models == {"model-a", "model-b"}

    # Both should be acquirable
    await limiter.acquire("model-a")
    await limiter.acquire("model-b")

    await limiter.stop()


async def test_rate_limiter_unconfigured_model_is_noop():
    limiter = RateLimiter({"model-a": 60})
    await limiter.start()

    # Should not block — no bucket for this model
    t0 = time.monotonic()
    await limiter.acquire("unknown-model")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1

    await limiter.stop()


async def test_rate_limiter_empty_config():
    limiter = RateLimiter()
    await limiter.start()
    await limiter.acquire("anything")  # no-op
    await limiter.stop()


async def test_rate_limiter_enforces_limit():
    limiter = RateLimiter({"slow": 60})  # 1 per second
    await limiter.start()

    t0 = time.monotonic()
    await limiter.acquire("slow")
    await limiter.acquire("slow")
    elapsed = time.monotonic() - t0

    # Second acquire should wait ~1s for the next token
    assert elapsed >= 0.8

    await limiter.stop()
