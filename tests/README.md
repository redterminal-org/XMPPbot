# XMPPBot Test Suite

This directory contains tests for the XMPPBot project.

## Structure

- `bot/` — tests for bot core and event handlers
- `database/` — tests for database managers and caching
- `plugins/` — plugin integration and plugin-specific logic
- `utils/` — utilities, helpers, rate limiting, etc.

## Running Tests

You should have `pytest` and `pytest-asyncio` installed.

```bash
pip install -r requirements-dev.txt
pytest
```

## Guidelines

- Write new tests under the appropriate directory.
- Use fixtures from `conftest.py` as needed.
- Place test config (test DB path, temp files) under `tests/`.
- Async tests: use `async def` with the `pytest.mark.asyncio` marker.
