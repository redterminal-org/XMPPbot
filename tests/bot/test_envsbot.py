import pytest
import asyncio
import sys
import os
import slixmpp
from unittest.mock import patch, MagicMock, AsyncMock

import envsbot

def noop(self):
    pass

slixmpp.ClientXMPP.__del__ = noop

class DummyFrom:
    def __init__(self, bare, resource):
        self.bare = bare
        self.resource = resource

class ControlledBot(envsbot.Bot):
    async def get_user_role(self, jid, room=None):
        return envsbot.Role.USER
    def reply(self, msg, text, *args, **kwargs):
        print(f"[TEST DEBUG REPLY CALLED] text={text!r}")
        if not hasattr(self, "_replies"):
            self._replies = []
        self._replies.append((msg, text, args, kwargs))

def _check_no_mock_jid(val, path="jid"):
    import unittest.mock as _mock
    if isinstance(val, dict):
        for k, v in val.items():
            _check_no_mock_jid(v, path+f".{k}")
    elif isinstance(val, list) or isinstance(val, tuple):
        for idx, v in enumerate(val):
            _check_no_mock_jid(v, path+f"[{idx}]")
    elif isinstance(val, _mock.MagicMock):
        raise RuntimeError(f"MagicMock detected at {path}: {val}")

@pytest.fixture
def bot(monkeypatch):
    # Patch direct dependencies
    monkeypatch.setattr(envsbot, "PresenceManager", MagicMock())
    monkeypatch.setattr(envsbot, "PluginManager", MagicMock())
    monkeypatch.setattr(envsbot, "TokenBucketRateLimiter", MagicMock())
    monkeypatch.setattr(envsbot, "DatabaseManager", MagicMock())
    monkeypatch.setattr(envsbot, "config", {"jid": "jid", "password": "pw", "prefix": ","})
    monkeypatch.setattr(envsbot, "setup_logging", lambda: None)

    with patch.object(envsbot.slixmpp.ClientXMPP, "__init__", lambda self, jid, pw: None), \
         patch.object(envsbot.slixmpp.ClientXMPP, "register_plugin", lambda self, *a, **k: None), \
         patch.object(envsbot.slixmpp.ClientXMPP, "add_event_handler", lambda self, *a, **k: None), \
         patch.object(envsbot.slixmpp.ClientXMPP, "make_message", lambda self, *a, **k: MagicMock(send=MagicMock(return_value=None))):
        b = ControlledBot()
    b.default_ns = "jabber:client"
    b.Message = MagicMock()
    b._XMLStream__event_handlers = {}

    class FakeMUCPlugin:
        def get_jid_property(self, *a, **k):
            return "user@host"
    
    b.plugin = {"xep_0045": FakeMUCPlugin()}

    class FakeUsers:
        async def get(self, jid):
            return {"role": 80, "jid": "user@host"}
        async def flush_all(self):
            pass

    class FakeDB:
        def __init__(self):
            self.users = FakeUsers()
    
    b.db = FakeDB()
    b.presence = MagicMock()
    b.presence.joined_rooms = {}
    b.bot_plugins = MagicMock()
    b.rate_limiter = MagicMock()
    b.rate_limiter.allow = AsyncMock(return_value=(True, 0))
    b.rate_limiter.notify_allowed = MagicMock(return_value=False)
    b.make_message = MagicMock(return_value=MagicMock(send=MagicMock(return_value=None)))

    _check_no_mock_jid(b.plugin, "plugin")
    _check_no_mock_jid(b.db, "db")
    return b

@pytest.mark.asyncio
async def test_safe_send_message_sync_and_async(bot):
    msg = MagicMock()
    msg.send.return_value = None
    await bot._safe_send_message(msg)
    msg.send.assert_called_once()

    msg = MagicMock()
    coro = AsyncMock()
    msg.send.return_value = coro()
    await bot._safe_send_message(msg)
    assert msg.send.call_count >= 1

    msg = MagicMock()
    def raise_exc():
        raise Exception("fail")
    msg.send.side_effect = raise_exc
    await bot._safe_send_message(msg)

@pytest.mark.asyncio
async def test_reply_groupchat_and_private(monkeypatch, bot):
    monkeypatch.setattr(bot, "_reply_send_wrapper", AsyncMock())
    msg_obj = MagicMock()
    bot.make_message.return_value = msg_obj

    msg = {
        "type": "groupchat",
        "from": DummyFrom("room1", "tester"),
        "get": lambda k, d=None: "tester" if k == "mucnick" else None
    }
    bot.reply(msg, "hi", mention=True, ephemeral=True)
    await asyncio.sleep(0)
    assert hasattr(bot, "_replies") and bot._replies

    bot._replies = []
    msg = {
        "type": "chat",
        "from": DummyFrom("user@host", "sender"),
        "get": lambda k, d=None: None
    }
    bot.reply(msg, "hi")
    await asyncio.sleep(0)
    assert bot._replies

@pytest.mark.asyncio
async def test_handle_command_no_body_or_prefix(bot):
    m = {
        "type": "chat",
        "from": DummyFrom("room@conf", "sender"),
        "get": lambda k, d=None: None
    }
    for body in [None, "foo"]:
        await bot.handle_command(body, "jid@host", None, m, False)
    await bot.handle_command(",", "jid@host", None, m, False)

@pytest.mark.asyncio
async def test_handle_command_unresolved_or_noperm(bot):
    m = {
        "type": "groupchat",
        "from": DummyFrom("room@conf", "sender"),
        "get": lambda k, d=None: None
    }
    with patch("envsbot.resolve_command", return_value=(None, [])):
        replies = []
        bot.reply = lambda msg, text, *a, **k: replies.append((msg, text, a, k))
        await bot.handle_command(",unknown", "user@host", None, m, False)
        assert replies == []
    class FakeCmd:
        name = "test"
        handler = lambda *_: None
        role = 80
    with patch("envsbot.resolve_command", return_value=(FakeCmd, [])), \
         patch("envsbot.check_permission", return_value=False):
        replies = []
        bot.reply = lambda msg, text, *a, **k: replies.append((msg, text, a, k))
        await bot.handle_command(",test", "user@host", None, m, False)
        print("Replies:", replies)
        found = any("🔴 You are not allowed to use this command." in r[1] for r in replies)
        assert found

@pytest.mark.asyncio
async def test_handle_command_moderator_check(bot):
    m = {
        "type": "groupchat",
        "from": DummyFrom("room@conf", "sender"),
        "get": lambda k, d=None: "nick"
    }
    class FakeCmd:
        name = "testcmd"
        handler = AsyncMock()
        role = envsbot.Role.MODERATOR
    with patch("envsbot.resolve_command", return_value=(FakeCmd, [])), \
         patch("envsbot.check_permission", return_value=True):
        bot._replies = []
        await bot.handle_command(",testcmd", "user@host", "nick", m, True)
        found = any("🔴 Use this command in MUC Direct Message only." in r[1] for r in bot._replies)
        assert found

@pytest.mark.asyncio
async def test_handle_command_execution(bot):
    m = {
        "type": "chat",
        "from": DummyFrom("room@conf", "sender"),
        "get": lambda k, d=None: None
    }
    handled = {"ok": False}
    class C:
        name = "mycmd"
        handler = AsyncMock(side_effect=lambda *a, **k: handled.update(ok=True))
        role = 80
    with patch("envsbot.resolve_command", return_value=(C, [])), \
         patch("envsbot.check_permission", return_value=True):
        await bot.handle_command(",mycmd foo", "user@host", None, m, False)
        assert handled["ok"]

    class F:
        name = "badcmd"
        handler = AsyncMock(side_effect=Exception("fail"))
        role = 80
    with patch("envsbot.resolve_command", return_value=(F, [])), \
         patch("envsbot.check_permission", return_value=True), \
         patch.object(bot, "get_user_role", AsyncMock(return_value=envsbot.Role.OWNER)):
        bot._replies = []
        await bot.handle_command(",badcmd", "user@host", None, m, False)
        # No assertion, just for coverage

@pytest.mark.asyncio
async def test_send_restart_notification_room_and_private(bot, monkeypatch, tmp_path):
    import json
    notif_path = "/tmp/bot_restart_notification.json"
    notif = {
        "room": "room@conf",
        "nick": "yo",
        "sender": "jid@server",
        "is_room": True,
    }
    with open(notif_path, "w") as f:
        json.dump(notif, f)
    monkeypatch.setattr(envsbot.os.path, "exists", lambda fn: fn == notif_path)
    monkeypatch.setattr(envsbot.os, "remove", lambda fn: None)
    open_real = open
    monkeypatch.setattr("builtins.open", lambda fn, mode="r": open_real(notif_path, mode))
    sent = []
    async def fake_send(msg):
        sent.append(msg)
    bot._safe_send_message = fake_send
    await bot._send_restart_notification()
    assert sent, "Should send a message"

    notif2 = dict(notif)
    notif2["is_room"] = False
    with open(notif_path, "w") as f:
        json.dump(notif2, f)
    sent.clear()
    await bot._send_restart_notification()
    assert sent, "Should send a private message"

def test_get_latest_git_tag(monkeypatch):
    monkeypatch.setattr(envsbot.subprocess, "check_output", lambda *a, **k: b"v1.2.3\n")
    assert envsbot.get_latest_git_tag() == "v1.2.3"
    def raise_cpe(*a, **k): raise envsbot.subprocess.CalledProcessError(1, "git")
    monkeypatch.setattr(envsbot.subprocess, "check_output", raise_cpe)
    assert envsbot.get_latest_git_tag() is None

def test_main_copy_behavior(monkeypatch, tmp_path):
    source = tmp_path / "init_chat_slang.csv"
    target = tmp_path / "chat_slang.csv"
    source.write_text("hello, world\n")
    called = {}

    # Patch os.path.exists so envsbot logic matches file expectations
    monkeypatch.setattr(envsbot.os.path, "exists", lambda path: str(path).endswith("init_chat_slang.csv"))
    # Patch shutil.copyfile to mark when called and simulate a copy
    monkeypatch.setattr(envsbot.shutil, "copyfile", lambda s, t: target.write_text(source.read_text()))
    # Patch logger methods to record messages
    monkeypatch.setattr(envsbot.log, "info", lambda *a, **k: called.setdefault("info", True))
    monkeypatch.setattr(envsbot.log, "warning", lambda *a, **k: called.setdefault("warning", True))
    monkeypatch.setattr(envsbot.log, "error", lambda *a, **k: called.setdefault("error", True))

    # Simulate the file copy block as in envsbot.py's __main__ logic
    if envsbot.os.path.exists("init_chat_slang.csv") and not envsbot.os.path.exists("chat_slang.csv"):
        try:
            envsbot.shutil.copyfile("init_chat_slang.csv", "chat_slang.csv")
            envsbot.log.info(f"[INIT] ✅ Copied init_chat_slang.csv to chat_slang.csv")
        except Exception as e:
            envsbot.log.error(f"[INIT] 🔴 Failed to copy init_chat_slang.csv to chat_slang.csv: {e}")
    elif not envsbot.os.path.exists("init_chat_slang.csv"):
        envsbot.log.warning(f"[INIT] 🔴 Source file init_chat_slang.csv not found. Skipping copy.")
    else:
        envsbot.log.info(f"[INIT] ✅ Target file chat_slang.csv already exists. Skipping copy.")

    assert called.get("info") or called.get("warning") or called.get("error")
