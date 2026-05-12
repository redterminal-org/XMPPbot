import pytest
import aiosqlite
import json

from database.rooms import Rooms


@pytest.mark.asyncio
async def test_rooms_init_add_get(tmp_path):
    db_path = tmp_path / "r.db"
    async with aiosqlite.connect(db_path) as sqlite:
        rooms = Rooms(sqlite)
        await rooms.init()
        await rooms.add("room@conference", "my-bot", autojoin=True)
        row = await rooms.get("room@conference")
        assert row is not None
        assert row[0] == "room@conference"
        assert row[1] == "my-bot"
        assert row[2] == 1  # autojoin


@pytest.mark.asyncio
async def test_rooms_update_and_delete(tmp_path):
    db_path = tmp_path / "r2.db"
    async with aiosqlite.connect(db_path) as sqlite:
        rooms = Rooms(sqlite)
        await rooms.init()
        await rooms.add("r2@c", "bot")
        await rooms.update("r2@c", nick="changed", autojoin=0)
        row = await rooms.get("r2@c")
        assert row[1] == "changed"
        assert row[2] == 0
        # now delete and check gone
        await rooms.delete("r2@c")
        gone = await rooms.get("r2@c")
        assert gone is None


@pytest.mark.asyncio
async def test_rooms_status_set_get_delete(tmp_path):
    db_path = tmp_path / "r3.db"
    async with aiosqlite.connect(db_path) as sqlite:
        rooms = Rooms(sqlite)
        await rooms.init()
        await rooms.add("room@conf", "nick")
        await rooms.status_set("room@conf", "greeting", "hello")
        val = await rooms.status_get("room@conf", "greeting")
        assert val == "hello"
        # Set a nested value
        await rooms.status_set("room@conf", "nested.inner", 123)
        val_nested = await rooms.status_get("room@conf", "nested.inner")
        assert val_nested == 123
        # Delete nested value
        await rooms.status_delete("room@conf", "nested.inner")
        assert await rooms.status_get("room@conf", "nested.inner") is None
        # Delete root key
        await rooms.status_delete("room@conf", "greeting")
        assert await rooms.status_get("room@conf", "greeting") is None


@pytest.mark.asyncio
async def test_rooms_list(tmp_path):
    db_path = tmp_path / "room-list.db"
    async with aiosqlite.connect(db_path) as sqlite:
        rooms = Rooms(sqlite)
        await rooms.init()
        await rooms.add("r1@x", "n1")
        await rooms.add("r2@x", "n2")
        rows = await rooms.list()
        ids = {r[0] for r in rows}
        assert "r1@x" in ids
        assert "r2@x" in ids
