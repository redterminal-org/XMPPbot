import asyncio
import json
import logging
import types
import pytest
import sys

pytestmark = pytest.mark.asyncio
# (pytest-asyncio required)

from database.users import PluginRuntimeStore
from database.manager import DatabaseManager  # <-- Fixed import

# Patch logging to silence noisy logs
logging.getLogger("plugins.users").setLevel(logging.CRITICAL)

# --------------------------
# Mock database and helpers
# --------------------------

class DummyCursor:
    def __init__(self, row):
        self.row = row
        self._iterated = False
    async def fetchone(self):
        return self.row
    async def fetchall(self):
        return [self.row] if self.row else []
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        pass

class DummyDB:
    def __init__(self):
        self.data = {}
        self.last_updated = {}
        self.execute_calls = []
    async def execute(self, query, params):
        self.execute_calls.append((query, params))
        # For SELECT "users_runtime" queries:
        if "SELECT data" in query:
            jid = params[0]
            val = self.data.get(jid, None)
            last_update = self.last_updated.get(jid, None)
            if val is not None:
                # Return as tuple (data, last_updated)
                return DummyCursor((val, last_update))
            else:
                return DummyCursor(None)
        # For UPDATE/INSERT operations, just record and pretend to accept.
        return DummyCursor(None)

class DummyUM:
    def __init__(self):
        self.db = DummyDB()
        self._runtime_cache = {}
        self._runtime_meta = {}
        self._dirty_runtime = set()

# --------------------------
# Fixtures
# --------------------------

@pytest.fixture
def dummy_um():
    return DummyUM()

@pytest.fixture
def plugin_store(dummy_um):
    return PluginRuntimeStore(dummy_um, "test_plugin")

def make_json_blob(plugin_name, value):
    """helper for plugin store layout"""
    return json.dumps({"plugins": {plugin_name: value}})

# --------------------------
# Tests
# --------------------------

@pytest.mark.asyncio
async def test_load_from_db_ok(plugin_store, dummy_um):
    jid = "user@domain"
    value = {"foo": "bar"}
    data_blob = make_json_blob(plugin_store.plugin_name, value)
    dummy_um.db.data[jid] = data_blob
    dummy_um.db.last_updated[jid] = "2021-03-11T11:11:11"

    # Should load the correct structure
    loaded = await plugin_store._load_from_db(jid)
    assert "plugins" in loaded
    assert loaded["plugins"][plugin_store.plugin_name] == value
    assert dummy_um._runtime_meta[jid] == "2021-03-11T11:11:11"

@pytest.mark.asyncio
async def test_load_from_db_blank(plugin_store, dummy_um):
    jid = "unknown@domain"
    loaded = await plugin_store._load_from_db(jid)
    assert loaded == {"plugins": {}}
    assert dummy_um._runtime_meta[jid] is None

@pytest.mark.asyncio
async def test_load_from_db_decoding_error(plugin_store, dummy_um, caplog):
    jid = "user@domain"
    dummy_um.db.data[jid] = "not a valid json"
    dummy_um.db.last_updated[jid] = "A"
    with caplog.at_level(logging.ERROR), caplog.at_level(logging.CRITICAL):
        loaded = await plugin_store._load_from_db(jid)
        assert loaded == {"plugins": {}}
        # Should not raise, but log failure

@pytest.mark.asyncio
async def test_ensure_cache_creates_structure(plugin_store, dummy_um):
    jid = "abc@domain"
    # No entry
    plugin_store._ensure_cache(jid)
    assert jid in dummy_um._runtime_cache
    assert "plugins" in dummy_um._runtime_cache[jid]

@pytest.mark.asyncio
async def test_get_and_set(plugin_store, dummy_um):
    jid = "u@d"
    # Simulate blank load
    dummy_um.db.data[jid] = json.dumps({"plugins": {}})
    # Should default to missing, then set
    result = await plugin_store.get(jid, "foo")
    assert result is None
    await plugin_store.set(jid, "foo", "bar")
    assert await plugin_store.get(jid, "foo") == "bar"

@pytest.mark.asyncio
async def test_set_and_get(plugin_store, dummy_um):
    jid = "u@d"
    await plugin_store.set(jid, "a", 123)
    v = await plugin_store.get(jid, "a")
    assert v == 123
    # Should register as dirty
    assert jid in dummy_um._dirty_runtime

@pytest.mark.asyncio
async def test_delete(plugin_store, dummy_um):
    jid = "test@domain"
    await plugin_store.set(jid, "toremove", 999)
    # Add another
    await plugin_store.set(jid, "keep", 1)
    await plugin_store.set(jid, "toremove", None)
    val = await plugin_store.get(jid, "toremove")
    assert val is None
    assert await plugin_store.get(jid, "keep") == 1

@pytest.mark.asyncio
async def test_default_value(plugin_store, dummy_um):
    jid = "def@domain"
    # PluginRuntimeStore.get does not take default=, will return None if not set
    result = await plugin_store.get(jid, "notset")
    assert result is None

@pytest.mark.asyncio
async def test_global(plugin_store, dummy_um):
    # This tests the global store (under the __GLOBAL__ jid)
    await plugin_store.set_global("globkey", {"a": 1})
    v = await plugin_store.get_global("globkey")
    assert v == {"a": 1}
    await plugin_store.set_global("globkey", None)
    v2 = await plugin_store.get_global("globkey")
    assert v2 is None

@pytest.mark.asyncio
async def test_set_and_get_multiple_fields(plugin_store, dummy_um):
    jid = "multi@domain"
    await plugin_store.set(jid, "foo", 1)
    await plugin_store.set(jid, "bar", 2)
    for field, exp in [("foo", 1), ("bar", 2)]:
        v = await plugin_store.get(jid, field)
        assert v == exp

@pytest.mark.asyncio
async def test_dirty_flag_on_set_and_delete(plugin_store, dummy_um):
    jid = "flag@domain"
    dummy_um._dirty_runtime.clear()
    await plugin_store.set(jid, "x", 42)
    assert jid in dummy_um._dirty_runtime
    await plugin_store.set(jid, "x", None)
    assert jid in dummy_um._dirty_runtime

@pytest.mark.asyncio
async def test_delete_field_nop(plugin_store, dummy_um):
    jid = "noop@domain"
    await plugin_store.set(jid, "notset", None)  # should not fail
    # Should not throw or error

@pytest.mark.asyncio
async def test_global_does_not_affect_user(plugin_store, dummy_um):
    # Set global, then regular user
    await plugin_store.set_global("shared", 12)
    await plugin_store.set("abc@foo", "shared", 99)
    # Confirm the difference
    v1 = await plugin_store.get_global("shared")
    v2 = await plugin_store.get("abc@foo", "shared")
    assert v1 == 12
    assert v2 == 99

@pytest.mark.asyncio
async def test_local_global_keys_dont_leak(plugin_store, dummy_um):
    # Set a jid-specific key, ensure it doesn't appear in global
    await plugin_store.set("user@else", "mykey", 42)
    g = await plugin_store.get_global("mykey")
    assert g is None

@pytest.mark.asyncio
async def test_set_json_value(plugin_store, dummy_um):
    jid = "user@json"
    val = {"complex": [1, 2, {"a": "b"}]}
    await plugin_store.set(jid, "blob", val)
    got = await plugin_store.get(jid, "blob")
    assert got == val

@pytest.mark.asyncio
async def test_no_unintended_attr(plugin_store):
    # PluginRuntimeStore should only have required attributes
    assert hasattr(plugin_store, "plugin_name")
    assert hasattr(plugin_store, "um")
    # Should not have public dicts for data storage
    for attr in ["data", "cache", "values"]:
        assert not hasattr(plugin_store, attr)
