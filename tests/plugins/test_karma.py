import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import plugins.karma as karma
from utils.command import Role

@pytest.fixture
def fake_bot():
    bot = MagicMock()
    # Simulate core interfaces and DB plugin access
    store = AsyncMock()
    store.get_global = AsyncMock(return_value={})
    bot.db.users.plugin = MagicMock(return_value=store)
    bot.db.users.plugin.return_value = store

    bot.reply = MagicMock()
    bot.prefix = ","
    bot.presence.emoji = lambda status: "😊"
    bot.presence.joined_rooms = {}

    # Simulate get_user_role (async)
    bot.get_user_role = AsyncMock(return_value=Role.USER)
    bot.boundjid = MagicMock()
    bot.boundjid.bare = "bot@xmpp.tld"

    # Simulate JOINED_ROOMS global
    bot.bot_plugins = MagicMock()
    bot.plugin = {}
    return bot

@pytest.fixture
def fake_groupchat_msg():
    class FakeFrom:
        def __init__(self, bare, resource):
            self.bare = bare
            self.resource = resource

    msg_obj = {
        "from": FakeFrom("room@conf.xmpp.tld", "Alice"),
        "type": "groupchat",
        "mucnick": "Alice",
        "body": "hello",
        "id": "msgid",
    }

    # allow attribute and item access for 'from'
    class FakeMsg(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)
        def __contains__(self, k):
            return dict.__contains__(self, k)
    msg = FakeMsg(msg_obj)
    return msg

@pytest.mark.asyncio
async def test_handle_room_toggle_command_on(fake_bot, fake_groupchat_msg):
    # Should delegate if subcommand is on|off|status
    args = ["on"]
    result = await karma.handle_room_toggle_command(
        fake_bot, fake_groupchat_msg, True, args,
        store_getter=karma.get_karma_store,
        key=karma.KARMA_ENABLED_KEY,
        label="Karma",
        storage="dict",
        log_prefix="[KARMA]",
    )
    assert result is True

@pytest.mark.asyncio
async def test_karma_command_usage_message(fake_bot, fake_groupchat_msg):
    # Enable plugin for room
    with patch("plugins.karma._is_public_muc", return_value=True), \
         patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)):
        await karma.karma_command(fake_bot, "jid@user", "Alice", [], fake_groupchat_msg, True)
    fake_bot.reply.assert_called()
    called_args = fake_bot.reply.call_args[0]
    assert "Usage" in called_args[1]

@pytest.mark.asyncio
async def test_karma_command_top_and_bottom(fake_bot, fake_groupchat_msg):
    # Enable karma
    with patch("plugins.karma._is_public_muc", return_value=True), \
         patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)):
        scores = {"Bob": 5, "Carol": -3, "Alice": 8}
        with patch("plugins.karma._get_room_scores", AsyncMock(return_value=scores)):
            await karma.karma_command(fake_bot, "jid@user", "Alice", ["top"], fake_groupchat_msg, True)
            await karma.karma_command(fake_bot, "jid@user", "Alice", ["bottom"], fake_groupchat_msg, True)
    top_reply = any("Karma top" in str(call[0][1]) for call in fake_bot.reply.call_args_list)
    bottom_reply = any("Karma bottom" in str(call[0][1]) for call in fake_bot.reply.call_args_list)
    assert top_reply
    assert bottom_reply

@pytest.mark.asyncio
async def test_karma_command_lookup_known_nick(fake_bot, fake_groupchat_msg):
    with patch("plugins.karma._is_public_muc", return_value=True), \
         patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)), \
         patch("plugins.karma._known_room_nicks", lambda room: ["Alice", "Bob"]), \
         patch("plugins.karma._get_room_scores", AsyncMock(return_value={"Alice": 10})), \
         patch("plugins.karma._canonical_nick", lambda room, n: "Alice"):
        await karma.karma_command(fake_bot, "jid@user", "Alice", ["Alice"], fake_groupchat_msg, True)
    fake_bot.reply.assert_called()
    msg = fake_bot.reply.call_args[0][1]
    # Output includes ZWNBSP in display name, so check for that
    assert "Alic\uFEFFe" in msg

@pytest.mark.asyncio
async def test_karma_command_lookup_unknown_nick(fake_bot, fake_groupchat_msg):
    with patch("plugins.karma._is_public_muc", return_value=True), \
         patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)), \
         patch("plugins.karma._known_room_nicks", lambda room: ["Bob"]), \
         patch("plugins.karma._canonical_nick", lambda room, n: "Unknown"):
        await karma.karma_command(fake_bot, "jid@user", "Alice", ["Foo"], fake_groupchat_msg, True)
    fake_bot.reply.assert_called()
    msg = fake_bot.reply.call_args[0][1]
    # Output includes ZWNBSP in sub_name for >1 char names
    assert "\uFEFF" in msg or "not currently in this room" in msg

@pytest.mark.asyncio
async def test_on_message_increments_karma(fake_bot, fake_groupchat_msg):
    # Setup room and allow events
    room = fake_groupchat_msg["from"].bare
    nick = fake_groupchat_msg["from"].resource
    karma.JOINED_ROOMS[room] = {"nicks": {nick: {}}}
    fake_groupchat_msg["body"] = "Bob++"
    with patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)), \
         patch("plugins.karma._known_room_nicks", lambda room: ["Bob"]), \
         patch("plugins.karma._get_room_scores", AsyncMock(return_value={"Bob": 2})), \
         patch("plugins.karma._set_room_scores", AsyncMock()), \
         patch("plugins.karma._actor_throttle_key", AsyncMock(return_value="room@conf.xmpp.tld:actor")), \
         patch("plugins.karma._check_throttle", AsyncMock(return_value=True)), \
         patch("plugins.karma._canonical_nick", lambda room, n: "Bob"):
        await karma.on_message(fake_bot, fake_groupchat_msg)
    fake_bot.reply.assert_called()
    # Bob as recipient, expect ZWNBSP addition
    reply_msg = fake_bot.reply.call_args[0][1]
    assert "Bo\uFEFFb" in reply_msg and "now has" in reply_msg

@pytest.mark.asyncio
async def test_on_message_throttle_block(fake_bot, fake_groupchat_msg):
    # Set up so throttle is hit
    room = fake_groupchat_msg["from"].bare
    nick = fake_groupchat_msg["from"].resource
    karma.JOINED_ROOMS[room] = {"nicks": {nick: {}}}
    fake_groupchat_msg["body"] = "Bob++"
    with patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)), \
         patch("plugins.karma._known_room_nicks", lambda room: ["Bob"]), \
         patch("plugins.karma._get_room_scores", AsyncMock(return_value={"Bob": 2})), \
         patch("plugins.karma._set_room_scores", AsyncMock()), \
         patch("plugins.karma._actor_throttle_key", AsyncMock(return_value="room@conf.xmpp.tld:actor")), \
         patch("plugins.karma._check_throttle", AsyncMock(return_value=False)), \
         patch("plugins.karma._canonical_nick", lambda room, n: "Bob"):
        await karma.on_message(fake_bot, fake_groupchat_msg)
    fake_bot.reply.assert_called()
    assert "recently gave karma" in fake_bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_on_message_self_karma_prevented(fake_bot, fake_groupchat_msg):
    # Alice tries Alice++
    room = fake_groupchat_msg["from"].bare
    nick = fake_groupchat_msg["from"].resource
    karma.JOINED_ROOMS[room] = {"nicks": {nick: {}}}
    fake_groupchat_msg["body"] = "Alice++"
    with patch("plugins.karma._is_enabled_for_room", AsyncMock(return_value=True)), \
         patch("plugins.karma._known_room_nicks", lambda room: ["Alice"]), \
         patch("plugins.karma._get_room_scores", AsyncMock(return_value={"Alice": 2})), \
         patch("plugins.karma._canonical_nick", lambda room, n: "Alice"):
        await karma.on_message(fake_bot, fake_groupchat_msg)
    # No reply, as self-karma is ignored
    fake_bot.reply.assert_not_called()

@pytest.mark.asyncio
async def test_format_ranking_empty_and_nonempty():
    assert karma._format_ranking([]) == "none yet"
    entries = [("Alice", 5), ("Bob", 2)]
    out = karma._format_ranking(entries)
    # Check that each entry is decorated with ZWNBSP
    assert "#1 Alic\uFEFFe" in out and "#2 Bo\uFEFFb" in out

def test_extract_karma_events():
    body = "Bob++ Carol-- and Bob++"
    room = "fake"
    with patch("plugins.karma._known_room_nicks", lambda room: ["Bob", "Carol"]):
        events = karma._extract_karma_events(body, room)
    assert events == [("Bob", 1), ("Carol", -1), ("Bob", 1)]

def test_format_entry():
    out = karma._format_entry(2, "Alice", 5)
    # Should include ZWNBSP before last character of name
    assert "#2 Alic\uFEFFe" in out

def test_canonical_nick_preference():
    # The real function sorts nicks by length (desc), then alpha
    # Here, just test that it prefers case-insensitive exact match
    room = "room"
    orig = karma._known_room_nicks
    try:
        with patch("plugins.karma._known_room_nicks", lambda r: ["bob", "Bob", "alice"]):
            ret = karma._canonical_nick(room, "Bob")
        assert ret == "Bob" or ret == "bob"
    finally:
        pass
