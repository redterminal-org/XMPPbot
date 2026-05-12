import pytest
import asyncio
from utils.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_token_bucket_basic_allows_right_away():
    limiter = TokenBucketRateLimiter(capacity=2)
    allowed, retry = await limiter.allow("u1")
    assert allowed
    allowed, retry = await limiter.allow("u1")
    assert allowed
    allowed, retry = await limiter.allow("u1")
    assert not allowed
    assert retry > 0


@pytest.mark.asyncio
async def test_token_bucket_refills_tokens_after_interval():
    limiter = TokenBucketRateLimiter(capacity=1, refill_interval=0.1)
    allowed, _ = await limiter.allow("A")
    assert allowed
    allowed, retry = await limiter.allow("A")
    assert not allowed
    await asyncio.sleep(0.11)
    allowed, _ = await limiter.allow("A")
    assert allowed


@pytest.mark.asyncio
async def test_multiple_clients_independent_buckets():
    limiter = TokenBucketRateLimiter(capacity=1)
    a1, _ = await limiter.allow("A")
    b1, _ = await limiter.allow("B")
    assert a1 and b1
    a2, _ = await limiter.allow("A")
    b2, _ = await limiter.allow("B")
    assert not a2 and not b2


@pytest.mark.asyncio
async def test_exponential_backoff_and_block(monkeypatch):
    # simulate fast denials to trigger block
    limiter = TokenBucketRateLimiter(
        capacity=1, deny_threshold=2, deny_window=0.5, base_block_seconds=0.2, max_block_seconds=0.5
    )
    # Deny first (bucket empty after 1 request)
    await limiter.allow("zz")
    await limiter.allow("zz")  # bucket is empty
    await limiter.allow("zz")  # denial #1
    await limiter.allow("zz")  # denial #2: triggers block
    allowed, retry = await limiter.allow("zz")
    assert not allowed
    assert 0.1 <= retry <= 0.5  # Should be blocked at least a bit


@pytest.mark.asyncio
async def test_token_bucket_blocks_and_refills():
    # Small capacity for fast test, quick refill
    limiter = TokenBucketRateLimiter(capacity=1, refill_interval=0.2)
    client = "user-blocking"

    # Use up the only token
    allowed, retry = await limiter.allow(client)
    assert allowed

    # Now bucket should be empty, so the next call should block
    allowed, retry = await limiter.allow(client)
    assert not allowed
    assert retry > 0

    # Wait for just less than the refill time: should *still* be blocked
    await asyncio.sleep(0.1)
    allowed, retry = await limiter.allow(client)
    assert not allowed

    # Wait further, to allow token to refill
    await asyncio.sleep(0.15)
    allowed, retry = await limiter.allow(client)
    assert allowed
