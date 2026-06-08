import asyncio
import logging
import aiosqlite

from .users import UserManager
from .rooms import Rooms

# logger for this module
log = logging.getLogger(__name__)


class DatabaseManager:
    """
    Central database manager.

    Handles the SQLite connection and exposes
    table managers for users and rooms.

    Also runs background tasks that periodically
    flush cached user data to the database.
    """

    def __init__(self, path: str, flush_interval: int = 60):

        self.path = path
        self.conn = None

        self.users = None
        self.rooms = None

        self.flush_interval = flush_interval

        self._flush_task = None
        self._running = False

    async def connect(self):
        """Open the database connection and initialize tables."""

        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row

        # ✅ ENABLE FOREIGN KEYS HERE (global, correct place)
        await self.conn.execute("PRAGMA foreign_keys = ON;")

        # (optional but clean)
        cursor = await self.conn.execute("PRAGMA foreign_keys;")
        row = await cursor.fetchone()
        if row["foreign_keys"] != 1:
            raise RuntimeError("Failed to enable foreign keys")

        self.users = UserManager(self.conn)
        self.rooms = Rooms(self.conn)

        await self.users.init()
        await self.rooms.init()

        # add asyncio sqlite3 stop event
        self._stop_event = asyncio.Event()

        # start background flush task
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        """Background loop that flushes data periodically with retry logic."""
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.flush_interval
                    )
                except asyncio.TimeoutError:
                    if self.users:
                        await self._flush_with_retry()
        finally:
            # final guaranteed flush with retry
            if self.users:
                await self._flush_with_retry()

    async def _flush_with_retry(self, max_retries: int = 3,
                                backoff: float = 1.0):
        """
        Flush with exponential backoff retry logic.

        Args:
            max_retries: Maximum number of retry attempts
            backoff: Initial backoff in seconds (exponential growth)
        """
        for attempt in range(max_retries):
            try:
                await self.users.flush_all()
                return  # Success
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = backoff * (2 ** attempt)
                    log.warning(
                        "[DatabaseManager] Flush attempt %d/%d failed, "
                        "retrying in %.1fs: %s",
                        attempt + 1, max_retries, wait_time, e
                    )
                    await asyncio.sleep(wait_time)
                else:
                    log.exception(
                        "[DatabaseManager] 🔴 Flush failed after %d attempts:"
                        " %s",
                        max_retries, e
                    )

    async def flush(self):
        """Manually flush cached data with retry logic."""
        if self.users:
            await self._flush_with_retry()

    async def close(self):
        """
        Stop background tasks, flush caches, and close the database.
        """

        # signal shutdown
        self._stop_event.set()

        if self._flush_task:
            await self._flush_task

        if self.conn:
            await self.conn.close()

    async def execute(self, query: str, params: tuple | None = None,
                      auto_commit: bool = True):
        """
        Execute a write query (INSERT/UPDATE/DELETE).

        Args:
            query: SQL query string
            params: Query parameters (optional)
            auto_commit: If True, automatically commits. If False, caller
            must commit

        When used within an explicit transaction (BEGIN...COMMIT),
        set auto_commit=False to prevent premature commits.
        """
        if params is None:
            params = ()

        cursor = await self.conn.execute(query, params)

        # Only commit when desired and not within a transaction
        if auto_commit:
            await self.conn.commit()

        return cursor

    async def fetch_one(self, query: str, params: tuple | None = None):
        """
        Execute a query and return a single row.
        """
        if params is None:
            params = ()

        async with self.conn.execute(query, params) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None
        return row

    async def fetch_all(self, query: str, params: tuple | None = None):
        """
        Execute a query and return all rows.
        """
        if params is None:
            params = ()

        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return rows
