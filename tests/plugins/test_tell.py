# File: tests/plugins/test_tell.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import plugins.tell as tell


@pytest.mark.asyncio
async def test_parse_nick_and_message_basic():
    # Works with simple case
    assert tell.parse_nick_and_message("Foo: bar") == ("Foo", "bar")
    # Handles missing colon
    assert tell.parse_nick_and_message("no colon here") == (None, None)
    # Handles spaces and trailing/leading
    assert tell.parse_nick_and_message(
        "  Some Name :  msg value ") == ("Some Name", "msg value")
    # Handles empty fields
    assert tell.parse_nick_and_message(": hi") == (None, None)
    assert tell.parse_nick_and_message("Who:  ") == (None, None)


@pytest.mark.asyncio
async def test_tell_store_and_fetch(tmp_path):
    bot = MagicMock()
    store = MagicMock()
    bot.db.users.plugin.return_value = store
    # Use AsyncMock!
    store.get = AsyncMock(return_value=[{"foo": "bar"}])
    store.set = AsyncMock()

    payload = {"recv_jid": "jidX", "message": "hello"}
    await tell.tell_store(bot, "jidX", payload)

    # ...
    store.get = AsyncMock(return_value=[payload])
    await tell.tell_fetch(bot, "jidX")
    store.set.assert_called_with("jidX", "tell_messages", [])


@pytest.mark.asyncio
async def test_tell_cmd_toggle(monkeypatch):
    bot = MagicMock()
    store = AsyncMock()
    bot.db.users.plugin.return_value = store
    # Test on/off/status branch via handle_room_toggle_command
    # Simulate handled
    monkeypatch.setattr(tell, "handle_room_toggle_command",
                        AsyncMock(return_value=True))
    # Simulate enabled room
    store.get_global.return_value = {"testroom": True}
    msg = {"from": MagicMock(bare="testroom"),
           "body": ",tell on", "type": "groupchat"}
    await tell.tell_cmd(bot, "jid", "nick", ["on"], msg, True)
    assert bot.reply.call_count == 0  # Should not reply when handled = True


@pytest.mark.asyncio
async def test_tell_cmd_dm_and_args(monkeypatch):
    # Should reject use in DM
    bot = MagicMock()
    store = AsyncMock()
    bot.db.users.plugin.return_value = store
    # Enable room
    store.get_global.return_value = {"abc@conf": True}
    msg = {"from": MagicMock(bare="abc@conf"),
           "body": ",tell foo: hi", "type": "chat"}
    await tell.tell_cmd(bot, "jid", "nick", ["foo:", "hi"], msg, False)
    assert bot.reply.call_args[0][1].startswith(
        "This command is only available in groupchats.")


@pytest.mark.asyncio
async def test_tell_cmd_parsing_error(monkeypatch):
    # Handles invalid parsing of nick/message
    bot = MagicMock()
    store = AsyncMock()
    bot.db.users.plugin.return_value = store
    store.get_global.return_value = {"room": True}
    msg = {
        "from": MagicMock(bare="room"),
        "body": ",tell : ",
        "type": "groupchat"
    }
    msg["mucnick"] = "sender"
    # Not a groupchat
    await tell.tell_cmd(bot, "jid", "sender", [], msg, True)
    assert bot.reply.call_args[0][1].startswith("Usage:")


@pytest.mark.asyncio
async def test_tell_cmd_user_not_found(monkeypatch):
    # When target nick isn't found
    bot = MagicMock()
    store = AsyncMock()
    bot.db.users.plugin.return_value = store
    store.get_global.return_value = {"room@room": True}
    msg = {
        "from": MagicMock(bare="room@room"),
        "body": ",tell someone: test",
        "type": "groupchat"
    }
    msg["mucnick"] = "sender"
    monkeypatch.setattr(tell, "parse_nick_and_message",
                        lambda v: ("someone", "test"))
    monkeypatch.setattr(tell, "get_jids_from_nick_index",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "plugins._core.get_jids_from_nick_index", AsyncMock(return_value=[]))
    await tell.tell_cmd(bot, "jid", "sender", ["someone:", "test"], msg, True)
    bot.reply.assert_called()
    assert "Could not find user 'someone'" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_tell_cmd_store(monkeypatch):
    # Happy path, stores a message
    bot = MagicMock()
    store = AsyncMock()
    bot.db.users.plugin.return_value = store
    store.get_global.return_value = {"r@r": True}
    msg = {
        "from": MagicMock(bare="r@r"),
        "body": ",tell other: test-m",
        "mucnick": "A",
        "type": "groupchat",
    }
    # Target nick found, sender nick found
    monkeypatch.setattr(tell, "parse_nick_and_message",
                        lambda v: ("other", "test-m"))
    monkeypatch.setattr(tell, "get_jids_from_nick_index",
                        AsyncMock(side_effect=[["jidB"], ["jidA"]]))
    monkeypatch.setattr("plugins._core.get_jids_from_nick_index",
                        AsyncMock(side_effect=[["jidB"], ["jidA"]]))
    monkeypatch.setattr(tell, "tell_store", AsyncMock())
    await tell.tell_cmd(bot, "jidA", "A", ["other:", "test-m"], msg, True)
    bot.reply.assert_called()
    assert "[TELL] I'll deliver your message to" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_deliver_tell_messages_single(monkeypatch):
    bot = MagicMock()
    store = AsyncMock()
    bot.db.users.plugin.return_value = store
    monkeypatch.setattr(tell, "get_jids_from_nick_index",
                        AsyncMock(return_value=["jid1"]))
    monkeypatch.setattr("plugins._core.get_jids_from_nick_index",
                        AsyncMock(return_value=["jid1"]))
    payload = {
        "recv_jid": "jid1",
        "send_jid": "jidX",
        "send_nick": "Envsi",
        "recv_nick": "Alpha",
        "message": "Secret",
        "timestamp": 1695000000,
    }
    monkeypatch.setattr(tell, "tell_fetch", AsyncMock(return_value=[payload]))
    # Return pytz.timezone object!
    monkeypatch.setattr(tell, "get_user_tzinfo", AsyncMock(
        return_value=pytz.timezone("UTC")))
    msg = {
        "from": MagicMock(bare="room@room"),
        "muc": {"nick": "Alpha"},
    }
    await tell.deliver_tell_messages(bot, msg)
    bot.reply.assert_called()
    last_msg = bot.reply.call_args[0][1]
    assert (payload["send_nick"] in last_msg and payload["recv_nick"]
            in last_msg and payload["message"] in last_msg)


@pytest.mark.asyncio
async def test_deliver_tell_messages_no_jid(monkeypatch):
    # Should do nothing if the nick lookup fails
    bot = MagicMock()
    monkeypatch.setattr(tell, "get_jids_from_nick_index",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "plugins._core.get_jids_from_nick_index", AsyncMock(return_value=[]))
    msg = {
        "from": MagicMock(bare="room@r"),
        "muc": {"nick": "nobody"},
    }
    await tell.deliver_tell_messages(bot, msg)
    bot.reply.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_tell_messages_no_messages(monkeypatch):
    # Should do nothing if no messages to deliver
    bot = MagicMock()
    monkeypatch.setattr(tell, "get_jids_from_nick_index",
                        AsyncMock(return_value=["jid"]))
    monkeypatch.setattr("plugins._core.get_jids_from_nick_index",
                        AsyncMock(return_value=["jid"]))
    monkeypatch.setattr(tell, "tell_fetch", AsyncMock(return_value=[]))
    msg = {
        "from": MagicMock(bare="room@r"),
        "muc": {"nick": "nobody"},
    }
    await tell.deliver_tell_messages(bot, msg)
    bot.reply.assert_not_called()


def test_on_load_registers_event(monkeypatch):
    bot = MagicMock()
    # Patch partial so we can check call
    with patch("plugins.tell.partial", lambda fn, arg: (fn, arg)):
        tell.on_load(bot)
        bot.bot_plugins.register_event.assert_called()
        bp = bot.bot_plugins
        event_name, event_type, event_func = bp.register_event.call_args[0]
        assert event_name == "tell_notify"
        assert event_type == "groupchat_presence"
