import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import types

import plugins.users as users_mod  # Always import the tested module


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.db = MagicMock()
    bot.db.users = MagicMock()
    bot.db.users.plugin = MagicMock()
    bot.bot_plugins = MagicMock()
    bot.bot_plugins.plugins = {}
    bot.reply = MagicMock()
    bot.get_user_role = AsyncMock(return_value=users_mod.Role.USER)
    return bot


@pytest.fixture
def mock_msg():
    m = MagicMock()
    m.get = MagicMock()
    m.__getitem__.side_effect = lambda k: m.__dict__.get(k, None)
    m.__setitem__.side_effect = lambda k, v: m.__dict__.__setitem__(k, v)
    m.body = ""
    m['from'] = MagicMock()
    m['from'].bare = "room@conference.server"
    m['from'].resource = "nick"
    m['muc'] = {"room": "room@conference.server", "nick": "nick"}
    m['type'] = "groupchat"
    return m


@pytest.fixture(autouse=True)
def patch_joined_rooms():
    with patch.object(users_mod, "JOINED_ROOMS", {}, create=True):
        yield


@pytest.fixture
def build_mock_bot():
    def factory():
        bot = MagicMock()
        bot.db = MagicMock()
        bot.db.users = MagicMock()
        bot.db.users.plugin = MagicMock()
        bot.bot_plugins = MagicMock()
        bot.bot_plugins.plugins = {}
        bot.reply = MagicMock()
        bot.get_user_role = AsyncMock(return_value=users_mod.Role.USER)
        return bot
    return factory


@pytest.mark.asyncio
async def test_on_muc_presence_adds_and_tracks_nick(mock_bot, mock_msg):
    pres = {
        "type": "available",
        "muc": {"room": "room1@conference.x", "nick": "john", "jid":
                MagicMock(bare="john@foo.bar")},
        "from": MagicMock(),
    }
    with patch("plugins.users.track_room_nick", new=AsyncMock()) as track, \
            patch("plugins.users.update_last_seen",
                  new=AsyncMock()) as last_seen:
        await users_mod.on_muc_presence(mock_bot, pres)
        track.assert_awaited()
        last_seen.assert_awaited()


@pytest.mark.asyncio
async def test_on_groupchat_message_updates_last_seen(mock_bot, mock_msg):
    bot_has_priv = MagicMock(return_value=True)
    mock_bot.bot_plugins.plugins = {'rooms': type(
        "RoomsPlugin", (), {"bot_has_privilege": bot_has_priv})()}
    mock_msg['muc'] = {"room": "room-A", "nick": "Nick"}
    mock_bot.plugin = {"xep_0045": MagicMock(
        get_jid_property=lambda r, n, s: "realjid@x")}
    with patch("plugins.users.update_last_seen",
               new=AsyncMock()) as update_last_seen:
        await users_mod.on_groupchat_message(mock_bot, mock_msg)
        update_last_seen.assert_awaited()


@pytest.mark.asyncio
async def test_track_room_nick(build_mock_bot):
    bot = build_mock_bot()
    bot.db.users.get = AsyncMock(return_value=None)
    bot.db.users.create = AsyncMock()
    plugin_store = AsyncMock()
    plugin_store.get = AsyncMock(return_value={})
    plugin_store.set = AsyncMock()
    bot.db.users.plugin.return_value = plugin_store
    bot.db.users._nick_index = {}
    bot.db.users._nick_index_lock = asyncio.Lock()
    await users_mod.track_room_nick(bot, "jid@x", "roomY", "nickname")
    assert plugin_store.set.await_count >= 1


@pytest.mark.asyncio
async def test_update_last_seen_newer_skipped(build_mock_bot):
    bot = build_mock_bot()
    bot.db.users.get = AsyncMock(
        return_value={"last_seen": "2099-01-01T01:01:01+00:00"})
    await users_mod.update_last_seen(bot, "jidtoignore@x")


@pytest.mark.asyncio
async def test_users_info_jid_and_nick(mock_bot, mock_msg):
    with patch.object(users_mod, "prefix", ","):
        # 1. Direct JID lookup
        mock_bot.db.users.get = AsyncMock(
            return_value={"jid": "user1@example.com",
                          "nickname": "N", "role": 4})
        with patch("plugins.users._send_user_info", new=AsyncMock()) as s_ui:
            await users_mod.users_info(mock_bot, "sender", "n",
                                       ["user1@example.com"], mock_msg, False)
            s_ui.assert_awaited()
        # 2. Fallback by nick, with single
        mock_bot.db.users.get = AsyncMock(
            side_effect=[None, {"jid": "user2@example.com",
                                "nickname": "M", "role": 4}])
        with patch("plugins.users.find_users_by_nick_safe",
                   new=AsyncMock(return_value=["user2@example.com"])), \
                patch("plugins.users._send_user_info",
                      new=AsyncMock()) as s_ui:
            await users_mod.users_info(mock_bot, "sender", "n", ["M"],
                                       mock_msg, False)
        # 3. Multiple users match by nick
        with patch("plugins.users.find_users_by_nick_safe",
                   new=AsyncMock(return_value=["a@e", "b@e"])), \
                patch.object(mock_bot, "reply") as bot_reply:
            await users_mod.users_info(mock_bot, "sender", "n", ["foo"],
                                       mock_msg, False)
            found = False
            for call in bot_reply.call_args_list:
                for arg in call[0]:
                    if "multiple users found" in str(arg).lower():
                        found = True
            assert found
        # 4. Edge: user not found
        mock_bot.db.users.get = AsyncMock(return_value=None)
        with patch.object(mock_bot, "reply") as bot_reply:
            await users_mod.users_info(mock_bot, "sender", "n",
                                       ["zzznotfound"], mock_msg, False)
        # 5. args missing
        with patch.object(mock_bot, "reply") as bot_reply:
            await users_mod.users_info(mock_bot, "sender", "n", [],
                                       mock_msg, False)
            found = False
            for call in bot_reply.call_args_list:
                for arg in call[0]:
                    if "usage:" in str(arg).lower():
                        found = True
            assert found


@pytest.mark.asyncio
async def test_users_list_shows_users(mock_bot, mock_msg):
    # Simulate room context
    users_mod.JOINED_ROOMS["room-A"] = {
        "nicks": {
            "A": {"jid": "a@example",
                  "affiliation": "member", "role": "user"},
            "B": {"jid": "b@example",
                  "affiliation": "member", "role": "admin"},
        }
    }
    # Patch to force visibility of rooms plugin with required attributes
    fake_rooms = type("RoomsPlugin", (), {
                      "JOINED_ROOMS": users_mod.JOINED_ROOMS})()
    mock_bot.bot_plugins.plugins = {"rooms": fake_rooms}
    mock_msg['from'].bare = "room-A"
    with (patch.object(users_mod, "prefix", ","),
          patch.object(mock_bot, "reply") as bot_reply):
        await users_mod.users_list(mock_bot, "send", "nick", [],
                                   mock_msg, False)
        found = False
        for call in bot_reply.call_args_list:
            for arg in call[0]:
                if "users in room-a" in str(arg).lower():
                    found = True
        assert found
    # No nicks
    users_mod.JOINED_ROOMS["room-B"] = {"nicks": {}}
    mock_msg['from'].bare = "room-B"
    fake_rooms = type("RoomsPlugin", (), {
                      "JOINED_ROOMS": users_mod.JOINED_ROOMS})()
    mock_bot.bot_plugins.plugins = {"rooms": fake_rooms}
    with (patch.object(users_mod, "prefix", ","),
          patch.object(mock_bot, "reply") as bot_reply):
        await users_mod.users_list(mock_bot, "send", "nick", ["room-B"],
                                   mock_msg, False)
        found = False
        for call in bot_reply.call_args_list:
            for arg in call[0]:
                if "no users found" in str(arg).lower():
                    found = True
        assert found


@pytest.mark.asyncio
async def test_users_role_permission_logic(mock_bot, mock_msg):
    with patch.object(users_mod, "prefix", ","), \
            patch(
            "plugins.users.JID",
            new=lambda x:
            types.SimpleNamespace(bare=x if isinstance(x, str) else str(x))):
        mock_bot.db.users.get = AsyncMock(
            return_value={"jid": "senderjid@example.com",
                          "role": users_mod.Role.ADMIN.value})
        mock_bot.get_user_role = AsyncMock(
            side_effect=[users_mod.Role.ADMIN, users_mod.Role.USER])
        mock_bot.db.users.set = AsyncMock()
        args = ["receiver@example.com", "user"]
        with patch.object(mock_bot, "reply"):
            await users_mod.users_update(mock_bot, "senderjid@example.com",
                                         "nick", args, mock_msg, False)
            assert mock_bot.db.users.set.await_count == 1


@pytest.mark.asyncio
async def test_users_delete_logic(mock_bot, mock_msg):
    with patch.object(users_mod, "prefix", ","):
        mock_bot.db.users.get = AsyncMock(
            return_value={"jid": "to@delete", "role": 7})
        mock_bot.db.users.delete = AsyncMock()
        args = ["to@delete"]
        with patch.object(mock_bot, "reply"):
            await users_mod.users_delete(mock_bot, "sender", "nick", args,
                                         mock_msg, False)
            mock_bot.db.users.delete.assert_awaited_with("to@delete")


@pytest.mark.asyncio
async def test_users_delete_errors(mock_bot, mock_msg):
    with patch.object(users_mod, "prefix", ","):
        mock_bot.db.users.get = AsyncMock(return_value=None)
        with patch.object(mock_bot, "reply") as bot_reply:
            await users_mod.users_delete(mock_bot, "s", "n", [],
                                         mock_msg, False)
            found = False
            for call in bot_reply.call_args_list:
                for arg in call[0]:
                    if "usage:" in str(arg).lower():
                        found = True
            assert found
        # Invalid JID
        args = ["invalidjid"]
        with patch.object(mock_bot, "reply") as bot_reply:
            await users_mod.users_delete(mock_bot, "s", "n", args,
                                         mock_msg, False)
        # User not found
        mock_bot.db.users.get = AsyncMock(return_value=None)
        args = ["notfound@x"]
        with patch.object(mock_bot, "reply") as bot_reply:
            await users_mod.users_delete(mock_bot, "s", "n",
                                         args, mock_msg, False)


@pytest.mark.asyncio
async def test__send_user_info_display_full(mock_bot, mock_msg):
    user_data = {
        "jid": "x@y", "nickname": "nn", "role": users_mod.Role.ADMIN.value,
        "created_at": "2024-01-01T01:00:00", "last_seen": "2024-05-01T17:00:00"
    }
    with patch.object(users_mod, "prefix", ","):
        await users_mod._send_user_info(mock_bot, mock_msg, user_data)
        assert mock_bot.reply.called

# ...add additional tests for track_room_nick, find_users_by_nick_safe, error
# and edge branches...
