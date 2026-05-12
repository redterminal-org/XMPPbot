import pytest


@pytest.mark.asyncio
async def test_rate_limiter_basic():
    from utils.rate_limiter import TokenBucketRateLimiter
    limiter = TokenBucketRateLimiter(capacity=2, refill_interval=0.1)
    allowed, _ = await limiter.allow("test-user")
    assert allowed
    allowed, _ = await limiter.allow("test-user")
    assert allowed
    allowed, _ = await limiter.allow("test-user")
    assert not allowed
