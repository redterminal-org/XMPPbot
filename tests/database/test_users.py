import pytest
import aiosqlite
import json
from database.users import UserManager, GLOBAL_JID


@pytest.mark.asyncio
async def test_users_init_and_create(tmp_path):
    db_path = tmp_path / "users.db"
    async with aiosqlite.connect(db_path) as sqlite:
        um = UserManager(sqlite)
        await um.init()
        await um.create("jid3@example.com", "nick3")
        await um.flush_all()
        user = await um.get("jid3@example.com")
        assert user["jid"] == "jid3@example.com"
        assert user["nickname"] == "nick3"
        assert user["role"] == 80


@pytest.mark.asyncio
async def test_users_set_and_update_last_seen(tmp_path):
    db_path = tmp_path / "users2.db"
    async with aiosqlite.connect(db_path) as sqlite:
        um = UserManager(sqlite)
        await um.init()
        await um.create("jid6@example.com", "nick6")
        await um.flush_all()
        user = await um.get("jid6@example.com")
        ts = user["last_seen"]
        # update last_seen
        await um.update_last_seen("jid6@example.com")
        await um.flush_all()
        user2 = await um.get("jid6@example.com")
        assert user2["last_seen"] != ts


@pytest.mark.asyncio
async def test_users_delete(tmp_path):
    db_path = tmp_path / "users3.db"
    async with aiosqlite.connect(db_path) as sqlite:
        um = UserManager(sqlite)
        await um.init()
        await um.create("jid7@example.com", "nick7")
        await um.flush_all()
        await um.delete("jid7@example.com")
        await um.flush_all()
        user = await um.get("jid7@example.com")
        assert user is None


@pytest.mark.asyncio
async def test_users_runtime_plugin(tmp_path):
    db_path = tmp_path / "users4.db"
    async with aiosqlite.connect(db_path) as sqlite:
        um = UserManager(sqlite)
        await um.init()
        await um.create("jid8@example.com", "nick8")
        store = um.plugin("test_plugin")
        # Set runtime data (plugin-specific)
        await store.set("jid8@example.com", "answer", 42)
        await um.flush_all()
        # Now get it back
        val = await store.get("jid8@example.com", "answer")
        assert val == 42
        # Set and get global
        await store.set_global("testkey", "globalval")
        await um.flush_all()
        gval = await store.get_global("testkey")
        assert gval == "globalval"
        # Clear plugin data
        await store.clear("jid8@example.com")
        await um.flush_all()
        remain = await store.get("jid8@example.com")
        assert remain == {}


@pytest.mark.asyncio
async def test_users_set_value_and_get_value(tmp_path):
    db_path = tmp_path / "users5.db"
    async with aiosqlite.connect(db_path) as sqlite:
        um = UserManager(sqlite)
        await um.init()
        cache = {}
        dirty = set()
        # Test nested set_value
        await um.set_value(cache, dirty, "jidx", "outer.inner", "deepvalue")
        assert "jidx" in dirty
        val = await um.get_value(cache["jidx"], "outer.inner")
        assert val == "deepvalue"
