import pytest
import aiosqlite

from database.manager import DatabaseManager


@pytest.mark.asyncio
async def test_database_manager_init_and_connect(tmp_db_path):
    db = DatabaseManager(tmp_db_path)
    await db.connect()
    # Check tables exist by querying PRAGMA
    tables = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table';"
    )
    table_names = {row['name'] for row in tables}
    assert "users" in table_names
    assert "users_runtime" in table_names
    assert "rooms" in table_names
    await db.close()


@pytest.mark.asyncio
async def test_database_manager_execute_fetch(tmp_db_path):
    db = DatabaseManager(tmp_db_path)
    await db.connect()
    # Insert a test row using execute
    await db.execute("INSERT INTO rooms (room_jid, nick) VALUES (?, ?)",
                     ("testroom@chat", "RoomBot"))
    # Fetch it back
    row = await db.fetch_one("SELECT * FROM rooms WHERE room_jid = ?",
                             ("testroom@chat",))
    assert row["room_jid"] == "testroom@chat"
    assert row["nick"] == "RoomBot"
    await db.close()


@pytest.mark.asyncio
async def test_database_manager_flush(tmp_db_path):
    db = DatabaseManager(tmp_db_path, flush_interval=0.1)
    await db.connect()
    # Add a user, triggers dirty cache in users manager
    await db.users.create("jid1@example.com", nickname="user1")
    await db.flush()
    row = await db.fetch_one("SELECT * FROM users WHERE jid=?",
                             ("jid1@example.com",))
    assert row["nickname"] == "user1"
    await db.close()


@pytest.mark.asyncio
async def test_database_manager_close_flushes(tmp_db_path):
    db = DatabaseManager(tmp_db_path)
    await db.connect()
    await db.users.create("jid2@example.com", nickname="test2")
    await db.close()
    # Assert data persisted after close
    async with aiosqlite.connect(tmp_db_path) as check_db:
        check_db.row_factory = aiosqlite.Row
        row = await check_db.execute("SELECT * FROM users WHERE jid=?",
                                     ("jid2@example.com",))
        result = await row.fetchone()
        assert result is not None
        assert result["nickname"] == "test2"
