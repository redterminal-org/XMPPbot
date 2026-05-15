import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import plugins.xmpp as xmpp

@pytest.fixture
def bot():
    """Mocked bot with plugin submodules and DB mock."""
    bot = MagicMock()
    bot.plugin = {
        "xep_0092": AsyncMock(),
        "xep_0012": AsyncMock(),
        "xep_0030": AsyncMock(),
        "xep_0199": AsyncMock(),
    }
    bot.db = MagicMock()
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={})
    bot.reply = MagicMock()
    return bot

@pytest.fixture
def msg(is_room=False):
    """Mocked message with minimal room/PM attributes"""
    m = MagicMock()
    m.__getitem__.side_effect = lambda k: {
        "from": MagicMock(bare="room@muc.example", resource="Nick"),
        "type": "groupchat" if is_room else "chat",
    }[k]
    m.get.side_effect = lambda k, default=None: {
        "type": "groupchat" if is_room else "chat",
        "from": MagicMock(bare="room@muc.example", resource="Nick"),
    }.get(k, default)
    m['from'].bare = "room@muc.example"
    m['from'].resource = "Nick"
    m['from'].__str__ = lambda *a: "room@muc.example/Nick"
    m.body = ""
    return m

@pytest.mark.asyncio
async def test_cmd_xmpp_toggle_on_off_status(bot, msg):
    # on/off/status delegated to handle_room_toggle_command
    with patch("plugins.xmpp.handle_room_toggle_command", new=AsyncMock(return_value=True)):
        for args in (["on"], ["off"], ["status"]):
            await xmpp.cmd_xmpp(bot, "you@server", "nick", args, msg, False)
            bot.reply.assert_not_called()
    # Unhandled returns usage
    with patch("plugins.xmpp.handle_room_toggle_command", new=AsyncMock(return_value=False)):
        await xmpp.cmd_xmpp(bot, "you@server", "nick", [], msg, False)
        bot.reply.assert_called_once()
        assert "Usage" in bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_cmd_xmpp_help_allowed(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    await xmpp.cmd_xmpp_help(bot, "jid", "nick", [], msg, True)
    bot.reply.assert_called()
    assert "XMPP Utility Commands" in bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_cmd_xmpp_help_denied(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={})
    await xmpp.cmd_xmpp_help(bot, "jid", "nick", [], msg, True)
    bot.reply.assert_not_called()

@pytest.mark.asyncio
async def test_cmd_xmpp_version_success(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    bot.plugin["xep_0092"].get_version.return_value.xml = [
        MagicMock(tag="{jabber:iq:version}query", __iter__=lambda self: iter([
            MagicMock(tag="{jabber:iq:version}name", text="Prosody"),
            MagicMock(tag="{jabber:iq:version}version", text="0.11.x"),
            MagicMock(tag="{jabber:iq:version}os", text="Debian Linux"),
        ]))
    ]
    await xmpp.cmd_xmpp_version(bot, "jid", "nick", ["example.org"], msg, True)
    bot.reply.assert_called_with(
        msg, pytest.approx("ℹ️ Version for example.org: **Prosody** v0.11.x on Debian Linux"), )

@pytest.mark.asyncio
async def test_cmd_xmpp_version_error(bot, msg):
    # Invalid domain, missing domain, IqTimeout, IqError, Exception
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    await xmpp.cmd_xmpp_version(bot, "jid", "nick", [], msg, True)
    bot.reply.assert_called_with(msg, "❌ Missing domain")
    bot.reply.reset_mock()
    await xmpp.cmd_xmpp_version(bot, "jid", "nick", ["foo"], msg, True)
    # 'foo' is not valid domain; error returned
    assert any("not a valid domain" in c[0][1] for c in bot.reply.call_args_list)
    bot.reply.reset_mock()
    # Simulate timeout
    bot.plugin["xep_0092"].get_version.side_effect = asyncio.TimeoutError()
    await xmpp.cmd_xmpp_version(bot, "jid", "nick", ["example.com"], msg, True)
    bot.reply.assert_called()
    # Simulate IqError
    from slixmpp.exceptions import IqError
    error_dict = {'error': {'condition': 'service-unavailable', 'text': '', 'type': ''}}
    bot.plugin["xep_0092"].get_version.side_effect = IqError(error_dict)
    await xmpp.cmd_xmpp_version(bot, "jid", "nick", ["example.com"], msg, True)
    # Simulate Exception
    bot.plugin["xep_0092"].get_version.side_effect = Exception("fail")
    await xmpp.cmd_xmpp_version(bot, "jid", "nick", ["example.com"], msg, True)
    bot.reply.assert_called()

@pytest.mark.asyncio
async def test_cmd_xmpp_uptime_success(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    bot.plugin["xep_0012"].get_last_activity.return_value = {'last_activity': {'seconds': 3661}}
    await xmpp.cmd_xmpp_uptime(bot, "jid", "nick", ["example.org"], msg, True)
    bot.reply.assert_called()
    assert "Uptime for example.org" in bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_cmd_xmpp_items_and_info(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    bot.plugin["xep_0030"].get_items.return_value = {'disco_items': {'items': [("room@conf", "A room")]}}
    await xmpp.cmd_xmpp_items(bot, "jid", "nick", ["xmpp.org"], msg, True)
    bot.reply.assert_called()
    assert "Items for" in bot.reply.call_args[0][1]
    # Info with identities/features
    bot.plugin["xep_0030"].get_info.return_value = {
        'disco_info': {
            'identities': [('server', 'im', 'XMPPServer')],
            'features': ['urn:xmpp:ping', 'urn:xmpp:mam'],
        }
    }
    await xmpp.cmd_xmpp_info(bot, "jid", "nick", ["xmpp.org"], msg, True)
    bot.reply.assert_called()
    assert "Identities" in bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_cmd_xmpp_contact(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    # XEP-0030 info with form and contact
    bot.plugin["xep_0030"].get_info.return_value = {
        'disco_info': {
            'form': [
                {'var': 'admin-address', 'value': ['admin@host']},
                {'var': 'abuse-address', 'value': ['abuse@host']}
            ]
        }
    }
    await xmpp.cmd_xmpp_contact(bot, "jid", "nick", ["xmpp.org"], msg, True)
    bot.reply.assert_called()
    assert "Contact info" in bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_cmd_xmpp_ping(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    bot.plugin["xep_0199"].ping = AsyncMock(return_value=None)
    await xmpp.cmd_xmpp_ping(bot, "jid", "nick", ["xmpp.org"], msg, True)
    bot.reply.assert_called()
    assert "Pong" in bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_cmd_xmpp_srv(bot, msg):
    # requires dnspython, which may not be installed in CI
    try:
        import dns.resolver
    except ImportError:
        pytest.skip("dnspython not installed")
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    await xmpp.cmd_xmpp_srv(bot, "jid", "nick", ["gmail.com"], msg, True)
    bot.reply.assert_called()

@pytest.mark.asyncio
async def test_cmd_xmpp_compliance(bot, msg):
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={"room@muc.example": True})
    # Patch aiohttp.ClientSession to mock network
    with patch("aiohttp.ClientSession.get") as mock_get:
        resp = AsyncMock()
        resp.status = 200
        class FakeSoup:
            def find(self, *a, **kw):
                class Score:
                    def get_text(self, **_): return "110/120"
                return Score()
        resp.text.return_value = "<html></html>"
        with patch("bs4.BeautifulSoup", return_value=FakeSoup()):
            mock_get.return_value.__aenter__.return_value = resp
            await xmpp.cmd_xmpp_compliance(bot, "jid", "nick", ["conversations.im"], msg, True)
            bot.reply.assert_called()
            assert "Compliance score" in "".join(str(a) for a in bot.reply.call_args[0])

@pytest.mark.asyncio
async def test_permission_denied(bot, msg):
    # If room/plugin not enabled, should not reply
    bot.db.users.plugin.return_value.get_global = AsyncMock(return_value={})
    funcs = [
        xmpp.cmd_xmpp_help, xmpp.cmd_xmpp_version, xmpp.cmd_xmpp_uptime,
        xmpp.cmd_xmpp_items, xmpp.cmd_xmpp_contact, xmpp.cmd_xmpp_info,
        xmpp.cmd_xmpp_ping, xmpp.cmd_xmpp_srv, xmpp.cmd_xmpp_compliance
    ]
    for func in funcs:
        bot.reply.reset_mock()
        await func(bot, "jid", "nick", ["example.com"], msg, True)
        bot.reply.assert_not_called()
