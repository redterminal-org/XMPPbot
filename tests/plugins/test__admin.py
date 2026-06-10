import pytest
import types
import builtins
import os
from datetime import datetime, timedelta

import plugins._admin as _admin

import pytest_asyncio  # <-- Add this


class Sender:
    def __str__(self): return "jid/sender"
    @property
    def bare(self): return "jid"


class DummyMsg:
    def __init__(self, groupchat=False):
        self.from_ = types.SimpleNamespace(bare="room@conf", resource="BOT")
        self.type = "groupchat" if groupchat else "chat"

    def __getitem__(self, key):
        if key == "from":
            return self.from_
        if key == "type":
            return self.type
        raise KeyError(key)

    def get(self, key, default=None):
        if key == "from":
            return self.from_
        if key == "type":
            return self.type
        return default
    # Attribute access fallback

    def __getattr__(self, key):
        if key == "from":
            return self.from_
        if key == "type":
            return self.type
        raise AttributeError(key)


@pytest_asyncio.fixture   # <-- Use this for async fixtures
async def fake_bot(monkeypatch):
    """
    Creates a fake bot object with all needed attributes for _admin commands.
    """
    class FakeDB:
        def __init__(self):
            self.closed = False
            self.path = "/tmp/test.db"

        async def close(self):
            self.closed = True

    class FakePlugins:
        @staticmethod
        def discover():
            return ["x", "y", "z"]
        plugins = {"foo": None, "bar": None}

    class FakeBound:
        def __str__(self): return "bot@domain"
    bot = types.SimpleNamespace()
    bot.disconnected = await _awaitable(True)
    bot.db = FakeDB()
    bot.prefix = ","
    bot.bot_plugins = FakePlugins()
    bot.boundjid = FakeBound()
    bot.reply = lambda msg, text, *a, **k: bot._replies.append((text, msg))
    bot._replies = []
    bot.disconnect = lambda: setattr(bot, "disco", True)
    bot.connection_start_time = datetime.now() - timedelta(hours=1,
                                                           minutes=3,
                                                           seconds=2)
    return bot


async def _awaitable(val):
    return None


def test_human_time():
    assert _admin.human_time(0) == "0s"
    assert _admin.human_time(-5) == "0s"
    assert _admin.human_time(61) == "1m 1s"
    assert _admin.human_time(3662) == "1h 1m 2s"
    assert _admin.human_time(3600*26+120+12) == "1d 2h 2m 12s"


def test_human_size():
    assert _admin.human_size(0) == "0 B"
    assert _admin.human_size(-1) == "unknown"
    assert _admin.human_size(1024) == "1.0 KB"
    assert _admin.human_size(1024*1024) == "1.0 MB"
    assert _admin.human_size(123456789) == "117.7 MB"
    assert _admin.human_size(int(1e12)) == "931.3 GB" or _admin.human_size(
        int(1e12)) == "931.3 GB"


def test_set_bot_start_time_sets_global():
    bot = object()
    _admin.BOT_START_TIME = None
    _admin.set_bot_start_time(bot)
    assert isinstance(_admin.BOT_START_TIME, datetime)
    old_time = _admin.BOT_START_TIME
    # Should not reset if called again
    _admin.set_bot_start_time(bot)
    assert _admin.BOT_START_TIME == old_time


@pytest.mark.asyncio
async def test_bot_status_success_and_all_fields(monkeypatch, fake_bot):
    _admin.BOT_START_TIME = datetime.now() - timedelta(hours=2)
    _admin.JOINED_ROOMS.clear()
    _admin.JOINED_ROOMS["room1"] = {"nick": "anon1"}
    _admin.JOINED_ROOMS["room2"] = {"nick": "anon2"}
    # Patch psutil
    monkeypatch.setattr(_admin, "psutil", types.SimpleNamespace(
        Process=lambda x=None: types.SimpleNamespace(
            memory_info=lambda: types.SimpleNamespace(rss=12*1024*1024),
            cpu_percent=lambda x: 42.0
        ),
        getloadavg=lambda: (1.23, 4.56, 7.89),
        cpu_count=lambda: 8
    ))
    monkeypatch.setattr(os.path, "getsize", lambda p: 12345)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    await _admin.bot_status(fake_bot, Sender(), "nick", [], DummyMsg(), False)
    replies = fake_bot._replies
    assert any(isinstance(r[0], list)
               and "🤖 Bot Status" in r[0][0] for r in replies)


@pytest.mark.asyncio
async def test_bot_status_handles_db_missing_and_errors(monkeypatch, fake_bot):
    fake_bot.db.path = None
    fake_bot.bot_plugins.discover = lambda: (
        _ for _ in ()).throw(ValueError("err"))
    # Patch psutil to throw
    monkeypatch.setattr(_admin, "psutil", types.SimpleNamespace(
        Process=lambda x=None: (_ for _ in ()).throw(ValueError("fail")),
        getloadavg=lambda: (_ for _ in ()).throw(ValueError("fail")),
        cpu_count=lambda: (_ for _ in ()).throw(ValueError("fail"))
    ))

    def raise_oserror_getsize(p):
        raise OSError()
    monkeypatch.setattr(os.path, "getsize", raise_oserror_getsize)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    _admin.JOINED_ROOMS.clear()
    await _admin.bot_status(fake_bot, Sender(), "nick", [], DummyMsg(), False)
    replies = fake_bot._replies
    assert any(isinstance(r[0], list) or "Failed" in r[0] for r in replies)


@pytest.mark.asyncio
async def test_bot_status_handles_exception(monkeypatch, fake_bot):
    monkeypatch.setattr(_admin, "set_bot_start_time", lambda b: (
        _ for _ in ()).throw(Exception("fail")))
    await _admin.bot_status(fake_bot, Sender(), "nick", [], DummyMsg(), False)
    replies = fake_bot._replies
    assert any("❌" in r[0] for r in replies)


@pytest.mark.asyncio
async def test_bot_shutdown_handles_errors(monkeypatch, fake_bot):
    fake_bot.disconnect = lambda: None

    class FakeDB:
        async def close(self): raise Exception("fail")
        path = "/tmp/foo"
    fake_bot.db = FakeDB()
    async def immediate_sleep(*args, **kwargs): return None
    monkeypatch.setattr(_admin.asyncio, "sleep", immediate_sleep)
    monkeypatch.setattr(_admin.asyncio, "wait_for", immediate_sleep)
    monkeypatch.setattr(_admin, "log",
                        types.SimpleNamespace(info=lambda *a, **k: None,
                                              error=lambda *a, **k: None,
                                              warning=lambda *a, **k: None))
    msg = DummyMsg()
    await _admin.bot_shutdown(fake_bot, Sender(), "nick", [], msg, False)


@pytest.mark.asyncio
async def test_on_load_sets_start_time(monkeypatch):
    called = []
    monkeypatch.setattr(_admin, "set_bot_start_time",
                        lambda b: called.append("set"))

    class FakeLogger:
        def info(self, *a, **k): called.append("info")
    monkeypatch.setattr(_admin, "log", FakeLogger())
    await _admin.on_load("bot")
    assert "set" in called and "info" in called
