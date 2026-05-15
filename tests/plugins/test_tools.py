import warnings
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import plugins.tools as tools

pytest_plugins = ("pytest_asyncio",)

@pytest.fixture
def bot():
    b = MagicMock()
    b.reply = MagicMock()
    b.db.users.plugin = AsyncMock(return_value=MagicMock())
    b.presence.emoji = MagicMock(return_value="😀")
    b.presence.status = {"show": "online", "status": "all good"}
    b.version = "1.2.3"
    b.prefix = ","
    b.plugin = {'xep_0045': MagicMock()}  # Only xep_0045 was referenced in your actual source
    b.presence.joined_rooms = {}
    return b

@pytest.fixture
def simple_msg():
    return {
        "from": MagicMock(bare="room@conf.org", resource="TestUser"),
        "mucnick": "TestUser",
        "body": "",
        "type": "groupchat"
    }

@pytest.fixture
def joined_rooms():
    return {
        "room@conf.org": {
            "nicks": {
                "TestUser": {"jid": "testuser@example.org"},
                "OtherGuy": {"jid": "otherguy@example.org"},
            }
        }
    }

@pytest.fixture
def enabled_rooms():
    return {"room@conf.org": True}

@pytest.mark.asyncio
async def test_ping_command_enabled_room(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", {})
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    await tools.ping_command(bot, "jid", "nick", [], simple_msg, True)
    bot.reply.assert_called_with(simple_msg, "🏓 Pong!", ephemeral=False)

@pytest.mark.asyncio
async def test_ping_command_disabled_room(bot, simple_msg, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", {})
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value={}))
    await tools.ping_command(bot, "jid", "nick", [], simple_msg, True)
    bot.reply.assert_called()
    assert "disabled" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_echo_command_success(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", {})
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    args = ["hello", "world!"]
    await tools.echo_command(bot, "jid", "nick", args, simple_msg, True)
    bot.reply.assert_called_with(simple_msg, "🔊 hello world!", ephemeral=False)

@pytest.mark.asyncio
async def test_echo_command_usage(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", {})
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    await tools.echo_command(bot, "jid", "nick", [], simple_msg, True)
    bot.reply.assert_called()
    assert "usage" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
@pytest.mark.parametrize("is_room,prefix,expected", [
    (True, ",", "⏰ Time for TestUser:"),
    (False, ",", "⏰ Time for testjid:"),
])
async def test_time_command_basic(bot, simple_msg, enabled_rooms, joined_rooms, monkeypatch, is_room, prefix, expected):
    monkeypatch.setattr(tools, "JOINED_ROOMS", joined_rooms)
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    monkeypatch.setattr(tools, "_get_user_timezone", AsyncMock(return_value="UTC"))
    msg = dict(simple_msg)
    if not is_room:
        msg['from'] = MagicMock(bare="testjid", resource="")
    await tools.time_command(bot, "jid", "TestUser", [], msg, is_room)
    bot.reply.assert_called()
    assert expected.lower() in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_time_command_invalid_nick(bot, simple_msg, enabled_rooms, joined_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", joined_rooms)
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    msg = dict(simple_msg)
    await tools.time_command(bot, "jid", "FakeNick", ["missingnick"], msg, True)
    bot.reply.assert_called()
    assert "not found" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_time_command_bad_timezone(bot, simple_msg, enabled_rooms, joined_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", joined_rooms)
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    monkeypatch.setattr(tools, "_get_user_timezone", AsyncMock(return_value="Fake/Zone"))
    msg = dict(simple_msg)
    await tools.time_command(bot, "jid", "TestUser", [], msg, True)
    bot.reply.assert_called()
    assert "utc" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
@pytest.mark.parametrize("is_room", [True, False])
async def test_date_command_basic(bot, simple_msg, enabled_rooms, joined_rooms, monkeypatch, is_room):
    monkeypatch.setattr(tools, "JOINED_ROOMS", joined_rooms)
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    monkeypatch.setattr(tools, "_get_user_timezone", AsyncMock(return_value="UTC"))
    msg = dict(simple_msg)
    if not is_room:
        msg['from'] = MagicMock(bare="testjid", resource="")
    await tools.date_command(bot, "jid", "TestUser", [], msg, is_room)
    bot.reply.assert_called()
    assert "📅 date for" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_utc_command(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    await tools.utc_command(bot, "jid", "nick", [], simple_msg, True)
    bot.reply.assert_called()
    assert "utc time" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_ts_command_valid(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", {"room@conf.org": {"nicks": {"TestUser": {"jid": "testuser@example.org"}}}})
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    monkeypatch.setattr(tools, "_get_user_timezone", AsyncMock(return_value="UTC"))
    await tools.timestamp_command(bot, "jid", "TestUser", ["1704067200"], simple_msg, True)
    bot.reply.assert_called()
    assert "⏰ timestamp 1704067200" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_ts_command_invalid(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    await tools.timestamp_command(bot, "jid", "TestUser", ["notanint"], simple_msg, True)
    bot.reply.assert_called()
    assert "invalid timestamp" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_ts_command_out_of_range(bot, simple_msg, enabled_rooms, monkeypatch):
    monkeypatch.setattr(tools, "JOINED_ROOMS", {"room@conf.org": {"nicks": {"TestUser": {"jid": "testuser@example.org"}}}})
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    monkeypatch.setattr(tools, "_get_user_timezone", AsyncMock(return_value="UTC"))
    await tools.timestamp_command(bot, "jid", "TestUser", ["-999999999999999"], simple_msg, True)
    bot.reply.assert_called()
    assert "invalid timestamp" in bot.reply.call_args[0][1].lower() or "out of range" in bot.reply.call_args[0][1].lower()

@pytest.mark.asyncio
async def test_seen_command_found(bot, simple_msg, enabled_rooms, joined_rooms, monkeypatch):
    async def _ret_list(bot, nick):
        return ["testuser@example.org"]
    async_mock = AsyncMock(side_effect=_ret_list)
    monkeypatch.setattr(tools, "get_jids_from_nick_index", async_mock)
    monkeypatch.setattr("plugins._core.get_jids_from_nick_index", async_mock)
    monkeypatch.setattr("plugins.tools.get_jids_from_nick_index", async_mock)
    monkeypatch.setattr(tools, "JOINED_ROOMS", joined_rooms)
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    bot.plugin["xep_0045"].get_roster.return_value = ["TestUser"]
    bot.plugin["xep_0045"].get_jid_property.return_value = "online"
    mock_user = {"last_seen": "2023-01-01T10:11:12+00:00"}
    bot.db.users.get = AsyncMock(return_value=mock_user)
    bot.presence.emoji = MagicMock(return_value="😀")
    await tools.seen_command(bot, "jid", "TestUser", [], simple_msg, True)
    bot.reply.assert_called()
    out = bot.reply.call_args[0][1]
    if isinstance(out, list):
        out = "\n".join(out)
    if "unexpected error" in out.lower():
        pytest.fail(f"Unexpected error in seen_command: {out}")
    assert "nickname" in out.lower()
    assert "last seen" in out.lower()
    await async_mock(bot, "TestUser")

@pytest.mark.asyncio
async def test_seen_command_not_found(bot, simple_msg, enabled_rooms, joined_rooms, monkeypatch):
    async def _ret_list(bot, nick):
        return []
    async_mock = AsyncMock(side_effect=_ret_list)
    monkeypatch.setattr(tools, "get_jids_from_nick_index", async_mock)
    monkeypatch.setattr("plugins._core.get_jids_from_nick_index", async_mock)
    monkeypatch.setattr("plugins.tools.get_jids_from_nick_index", async_mock)
    monkeypatch.setattr(tools, "JOINED_ROOMS", joined_rooms)
    monkeypatch.setattr(tools, "_get_enabled_rooms", AsyncMock(return_value=enabled_rooms))
    bot.db.users.get = AsyncMock(return_value=None)
    await tools.seen_command(bot, "jid", "UnknownNick", ["UnknownNick"], simple_msg, True)
    bot.reply.assert_called()
    out = bot.reply.call_args[0][1].lower()
    assert "no data found" in out or "not found" in out
    await async_mock(bot, "UnknownNick")
