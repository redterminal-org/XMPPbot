import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import types

from tests.helpers import PresenceStub

# Patch the logging to avoid noisy output
import logging
logging.getLogger("plugins.rooms").setLevel(logging.CRITICAL)

# Import the module under test
import plugins.rooms as rooms

@pytest.fixture(autouse=True)
def cleanup_joined_rooms():
    """Ensure JOINED_ROOMS is clean for each test."""
    orig = dict(rooms.JOINED_ROOMS)
    rooms.JOINED_ROOMS.clear()
    yield
    rooms.JOINED_ROOMS.clear()
    rooms.JOINED_ROOMS.update(orig)

@pytest.fixture
def fake_bot():
    bot = MagicMock()
    bot.boundjid.bare = "bot@domain"
    bot.boundjid.resource = "BotNick"
    bot.presence.status = {'show': 'chat', 'status': 'online'}
    bot.presence.joined_rooms = {}
    # plugins registry, used by on_load
    bot.bot_plugins = MagicMock()
    # plugin system
    bot.plugin = {"xep_0045": MagicMock()}
    # DB interface
    bot.db = MagicMock()
    bot.db.rooms = MagicMock()
    bot.db.users = MagicMock()
    bot.prefix = "!"
    bot.reply = MagicMock()
    bot.presence.broadcast = MagicMock()
    return bot

@pytest.fixture
def fake_msg():
    msg = {
        "from": MagicMock(),
        "type": "groupchat",
        "to": MagicMock(),
    }
    msg["from"].bare = "room@conference.test"
    msg["from"].resource = "Nick"
    msg["to"].bare = "bot@domain"
    return msg

@pytest.mark.asyncio
async def test_is_nick_change_true_and_false():
    pres = MagicMock()
    stat1 = MagicMock()
    stat2 = MagicMock()
    stat1.attrib.get.return_value = "303"
    stat2.attrib.get.return_value = "100"
    pres.xml.findall.return_value = [stat2, stat1]
    assert rooms.is_nick_change(pres) is True

    stat1.attrib.get.return_value = "100"
    stat2.attrib.get.return_value = "200"
    pres.xml.findall.return_value = [stat1, stat2]
    assert rooms.is_nick_change(pres) is False

@pytest.mark.asyncio
async def test_on_muc_presence_join_or_leave(fake_bot):
    bot_room = "room@conference.test"
    bot_nick = "BotNick"
    user_nick = "Nick"
    fake_bot.boundjid.bare = "bot@domain"

    # Minimal stub helpers
    class FromJID:
        def __init__(self, bare, resource):
            self.bare = bare
            self.resource = resource

    class FakeJID:
        def __init__(self, bare):
            self.bare = bare

    class FakeMuc:
        def __init__(self, values):
            self._values = values
        def get(self, k):
            return self._values.get(k)

    from_jid = FromJID(bot_room, bot_nick)

    # 1. Bot joins
    from_jid.resource = bot_nick
    pres = PresenceStub(
        from_=from_jid,
        muc=FakeMuc({
            "role": "moderator",
            "jid": FakeJID(fake_bot.boundjid.bare),
            "affiliation": "admin"
        }),
        type="available"
    )
    await rooms.on_muc_presence(fake_bot, pres)
    assert bot_room in rooms.JOINED_ROOMS
    assert bot_nick in rooms.JOINED_ROOMS[bot_room]["nicks"]

    # 2. User joins
    from_jid.resource = user_nick
    pres = PresenceStub(
        from_=from_jid,
        muc=FakeMuc({
            "role": "participant",
            "jid": FakeJID("user@jid"),
            "affiliation": "member"
        }),
        type="available"
    )
    await rooms.on_muc_presence(fake_bot, pres)
    assert user_nick in rooms.JOINED_ROOMS[bot_room]["nicks"]

    # 3. User leaves
    from_jid.resource = user_nick
    pres = PresenceStub(
        from_=from_jid,
        muc=FakeMuc({
            "role": "participant",
            "jid": FakeJID("user@jid"),
            "affiliation": "member"
        }),
        type="unavailable"
    )
    await rooms.on_muc_presence(fake_bot, pres)
    assert user_nick not in rooms.JOINED_ROOMS[bot_room]["nicks"]

    # 4. Bot leaves
    from_jid.resource = bot_nick
    pres = PresenceStub(
        from_=from_jid,
        muc=FakeMuc({
            "role": "moderator",
            "jid": FakeJID(fake_bot.boundjid.bare),
            "affiliation": "admin"
        }),
        type="unavailable"
    )
    await rooms.on_muc_presence(fake_bot, pres)
    assert bot_room not in rooms.JOINED_ROOMS

@pytest.mark.asyncio
async def test_bot_has_privilege():
    rooms.JOINED_ROOMS["room"] = {"affiliation": "owner"}
    assert rooms.bot_has_privilege("room") is True
    rooms.JOINED_ROOMS["room"] = {"affiliation": "member"}
    assert rooms.bot_has_privilege("room") is False
    assert rooms.bot_has_privilege("room_notexist") is False

@pytest.mark.asyncio
async def test_room_status_helpers(fake_bot):
    fake_bot.db.rooms.status_get = AsyncMock(return_value={"a": 1})
    assert await rooms.room_status_get(fake_bot, "room", "a") == {"a": 1}
    fake_bot.db.rooms.status_set = AsyncMock()
    await rooms.room_status_set(fake_bot, "room", "x", 123)
    fake_bot.db.rooms.status_set.assert_called_with("room", "x", 123)
    fake_bot.db.rooms.status_delete = AsyncMock()
    await rooms.room_status_delete(fake_bot, "room", "p")
    fake_bot.db.rooms.status_delete.assert_called_with("room", "p")

@pytest.mark.asyncio
async def test_is_valid_room_jid_success(fake_bot, fake_msg):
    with patch("plugins.rooms.is_valid_muc_domain", AsyncMock(return_value=True)):
        jid = "room@conference.domain"
        assert await rooms.is_valid_room_jid(fake_bot, jid, fake_msg) is True

@pytest.mark.asyncio
async def test_is_valid_room_jid_failures(fake_bot, fake_msg):
    with patch("plugins.rooms.is_valid_muc_domain", AsyncMock(return_value=False)):
        assert await rooms.is_valid_room_jid(fake_bot, "room/conference", fake_msg) is False
        assert await rooms.is_valid_room_jid(fake_bot, "room", fake_msg) is False
        assert await rooms.is_valid_room_jid(fake_bot, "@domain", fake_msg) is False
        # Simulate failed domain check
        assert await rooms.is_valid_room_jid(fake_bot, "room@domain", fake_msg) is False

@pytest.mark.asyncio
async def test_autojoin_rooms(fake_bot):
    fake_bot.db.rooms.list = AsyncMock(
        return_value=[("room1@conf", "BotNick", True, "joined"),
                      ("room2@conf", "BotNick", False, "left")]
    )
    fake_bot.plugin["xep_0045"].join_muc = AsyncMock()
    await rooms.autojoin_rooms(fake_bot)
    assert "room1@conf" in rooms.JOINED_ROOMS
    assert "room2@conf" not in rooms.JOINED_ROOMS

@pytest.mark.asyncio
async def test_set_room_control_defaults(fake_bot):
    # All plugins -> dict
    room = "test@conf"
    fake_bot.db.users.plugin = lambda plugin: types.SimpleNamespace(
        get_global=AsyncMock(return_value={}),
        set_global=AsyncMock())
    await rooms.set_room_control_defaults(fake_bot, room)

@pytest.mark.asyncio
async def test_cmd_room_setdefaults(fake_bot, fake_msg):
    # Not in joined rooms
    await rooms.cmd_room_setdefaults(fake_bot, "jid", "nick", [], fake_msg, False)
    # Now simulate the room present and in DB
    room_jid = fake_msg["from"].bare
    rooms.JOINED_ROOMS[room_jid] = {}
    fake_bot.db.rooms.get = AsyncMock(return_value=(room_jid, "BotNick", True, None))
    with patch("plugins.rooms.set_room_control_defaults", AsyncMock()):
        await rooms.cmd_room_setdefaults(fake_bot, "jid", "nick", [], fake_msg, False)
        # Error case: trigger inside the try/except block!
        with patch("plugins.rooms.set_room_control_defaults", AsyncMock(side_effect=Exception("fail-setdefaults"))):
            await rooms.cmd_room_setdefaults(fake_bot, "jid", "nick", [], fake_msg, False)
            # Verify reply called with error
            assert any("Error restoring defaults" in str(call.args[1]) for call in fake_bot.reply.mock_calls)

@pytest.mark.asyncio
async def test_cmd_room_plugins(fake_bot, fake_msg):
    room_jid = fake_msg["from"].bare
    rooms.JOINED_ROOMS[room_jid] = {}
    fake_bot.db.users.plugin = lambda plugin: types.SimpleNamespace(
        get_global=AsyncMock(return_value={})
    )
    await rooms.cmd_room_plugins(fake_bot, "jid", "nick", [], fake_msg, False)

@pytest.mark.asyncio
async def test_rooms_add(fake_bot, fake_msg):
    fake_bot.db.rooms.get = AsyncMock(return_value=None)
    fake_bot.db.rooms.add = AsyncMock()
    with patch("plugins.rooms.set_room_control_defaults", AsyncMock()):
        with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
            msg = dict(fake_msg)
            msg["from"].bare = "room@conference.domain"
            await rooms.rooms_add(fake_bot, "s", "s", ["room@conference.domain", "BotNick"], msg, False)

@pytest.mark.asyncio
async def test_rooms_add_already_exists(fake_bot, fake_msg):
    fake_bot.db.rooms.get = AsyncMock(return_value=(1,2,3,4))
    with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
        await rooms.rooms_add(fake_bot, "s", "s", ["room@conference.domain", "BotNick"], fake_msg, False)

@pytest.mark.asyncio
async def test_rooms_update(fake_bot, fake_msg):
    fake_bot.db.rooms.update = AsyncMock()
    with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
        await rooms.rooms_update(fake_bot, "jid", "nick",
            ["room@conference.domain", "nick", "OtherBot"], fake_msg, False)
        await rooms.rooms_update(fake_bot, "jid", "nick",
            ["room@conference.domain", "autojoin", "yes"], fake_msg, False)
        await rooms.rooms_update(fake_bot, "jid", "nick",
            ["room@conference.domain", "badfield", "xxx"], fake_msg, False)

@pytest.mark.asyncio
async def test_rooms_delete(fake_bot, fake_msg):
    fake_bot.db.rooms.get = AsyncMock(return_value=(1, 2, 3, 4))
    fake_bot.db.rooms.delete = AsyncMock()
    with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
        # room joined
        room_jid = "room@conference.domain"
        rooms.JOINED_ROOMS[room_jid] = {"nick": "BotNick"}
        fake_bot.presence.joined_rooms[room_jid] = "BotNick"
        await rooms.rooms_delete(fake_bot, "jid", "nick", [room_jid], fake_msg, False)
        # room not joined, but in db
        rooms.JOINED_ROOMS[room_jid] = {"nick": "BotNick"}  # Restore for test coverage, since previous call popped it
        await rooms.rooms_delete(fake_bot, "jid", "nick", [room_jid], fake_msg, False)
        # DB removal failure
        fake_bot.db.rooms.get = AsyncMock(side_effect=Exception("db error"))
        await rooms.rooms_delete(fake_bot, "jid", "nick", [room_jid], fake_msg, False)

@pytest.mark.asyncio
async def test_rooms_list(fake_bot):
    fake_bot.db.rooms.list = AsyncMock(return_value=[
        ("room@conference.a", "nick1", True, "stat1"),
        ("room@conference.b", "nick2", False, "{}")
    ])
    rooms.JOINED_ROOMS["room@conference.a"] = {
        "nick": "nick1", "affiliation": "admin", "role": "owner", "autojoin": True, "status": "stat1"
    }
    await rooms.rooms_list(fake_bot, "jid", "nick", [], MagicMock(), False)
    # Test with no rows
    fake_bot.db.rooms.list = AsyncMock(return_value=[])
    await rooms.rooms_list(fake_bot, "jid", "nick", [], MagicMock(), False)

@pytest.mark.asyncio
async def test_rooms_join_leave_and_sync(fake_bot, fake_msg):
    fake_bot.db.rooms.get = AsyncMock(return_value=None)
    fake_bot.db.rooms.add = AsyncMock()
    fake_bot.plugin["xep_0045"].join_muc = AsyncMock()
    fake_bot.presence.joined_rooms = {}
    with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
        await rooms.rooms_join(fake_bot, "jid", "nick", ["room@conf", "BotNick"], fake_msg, False)
        await rooms.rooms_join(fake_bot, "jid", "nick", ["room@conf"], fake_msg, False)

    # leave
    room_jid = "room@conf"
    rooms.JOINED_ROOMS[room_jid] = {"nick": "BotNick"}
    fake_bot.presence.joined_rooms[room_jid] = "BotNick"
    fake_bot.plugin["xep_0045"].leave_muc = MagicMock()
    with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
        await rooms.rooms_leave(fake_bot, "jid", "nick", [room_jid], fake_msg, False)

    # sync
    fake_bot.db.rooms.list = AsyncMock(return_value=[
        ("room@c1", "Bot", True, "state1"),
        ("room@c2", "Bot2", False, "state2"),
    ])
    fake_bot.plugin["xep_0045"].join_muc = AsyncMock()
    rooms.JOINED_ROOMS["room@c1"] = {"nick": "Bot"}
    with patch("plugins.rooms.is_valid_room_jid", AsyncMock(return_value=True)):
        await rooms.rooms_sync(fake_bot, "jid", "nick", [], fake_msg, False)

@pytest.mark.asyncio
async def test_is_valid_muc_domain_true_false(fake_bot):
    xmpp_plugin = MagicMock()
    xmpp_plugin.get_info = AsyncMock(return_value={"disco_info": {"features": ["http://jabber.org/protocol/muc"]}})
    fake_bot.__getitem__.return_value = xmpp_plugin
    fake_bot.__getitem__ = MagicMock(return_value=xmpp_plugin)
    assert await rooms.is_valid_muc_domain(fake_bot, "conference.domain")
    xmpp_plugin.get_info = AsyncMock(side_effect=Exception("fail"))
    assert not await rooms.is_valid_muc_domain(fake_bot, "conference.domain")
