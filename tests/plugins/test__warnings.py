import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_asyncmock_warning():
    async def my_coro(x, y): return [x, y]
    m = AsyncMock(side_effect=my_coro)
    result = await m(1, 2)
    assert result == [1, 2]
