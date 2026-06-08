import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, Mock
import plugins.weather as weather

# --- Support patching of weather.JOINED_ROOMS ---


@pytest_asyncio.fixture(autouse=True)
def patch_joined_rooms(monkeypatch):
    join_data = {
        "testroom@conference.example.com": {
            "nicks": {
                "Alice": {"jid": "alice@example.com"},
                "Bob": {"jid": "bob@example.com"},
            }
        }
    }
    monkeypatch.setattr(weather, "JOINED_ROOMS", join_data)


@pytest_asyncio.fixture(autouse=True)
def patch_config(monkeypatch):
    class DummyConfig(dict):
        def get(self, key, default=None):
            return self[key] if key in self else default
    cfg = DummyConfig({"weather_api_key": "TESTKEY", "prefix": ","})
    monkeypatch.setattr(weather, "config", cfg)


@pytest_asyncio.fixture
def fake_bot():
    class DummyStore:
        async def get(self, jid, key, default=None): return None
        async def set(self, *a, **k): pass
        async def get_global(self, k, default=None): return {}

    class DummyUsers:
        def plugin(self, _): return DummyStore()

    class DummyDB:
        users = DummyUsers()
    bot = Mock()
    bot.db = DummyDB()
    bot.bot_plugins = Mock()
    bot.plugin = {}
    bot.presence = Mock()
    bot.presence.emoji = lambda status: "😀"
    bot.reply = Mock()
    return bot


@pytest_asyncio.fixture
def fake_msg():
    """Standard groupchat message with mucnick."""
    return {
        "from": Mock(bare="testroom@conference.example.com", resource="Alice"),
        "body": ",weather",
        "mucnick": "Alice",
        "type": "groupchat"
    }

# Patch plumbing helpers from _core and our own DB


@pytest_asyncio.fixture(autouse=True)
def patch_plugins(monkeypatch):
    monkeypatch.setattr(
        weather._core, "handle_room_toggle_command",
        AsyncMock(return_value=False))
    monkeypatch.setattr(weather._core, "_get_enabled_rooms", AsyncMock(
        return_value={"testroom@conference.example.com": True}))
    monkeypatch.setattr(weather._core, "_is_muc_pm", lambda msg: False)
    monkeypatch.setattr(weather, "get_weather_store",
                        AsyncMock(return_value=Mock()))


@pytest_asyncio.fixture
def patch_aiohttp(monkeypatch):
    """Patch aiohttp.ClientSession to return a weather string."""
    class DummyResp:
        status = 200

        async def text(self):
            return "Berlin: Sunny 21°C 🌤️"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class DummyAiohttpSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def get(self, *a, **k): return DummyResp()
    monkeypatch.setattr(weather, "aiohttp", Mock(
        ClientSession=DummyAiohttpSession))


@pytest_asyncio.fixture(autouse=True)
def patch_vcard(monkeypatch):
    # Default: LOCALITY=Berlin, to satisfy minimum weather lookups
    monkeypatch.setattr(weather.vcard, "get_user_vcard",
                        AsyncMock(return_value={"LOCALITY": "Berlin"}))
    monkeypatch.setattr(weather.vcard, "vcard_field",
                        AsyncMock(return_value="Berlin"))


def output_of_reply(reply):
    out = reply.call_args[0][1]
    if isinstance(out, list):
        out = ' '.join(out)
    return out


@pytest.mark.asyncio
async def test_weather_command_happy_path(fake_bot, fake_msg,
                                          patch_plugins, patch_aiohttp):
    await weather.weather_command(fake_bot, "jid", "Alice", [],
                                  fake_msg, True)
    fake_bot.reply.assert_called()
    out = output_of_reply(fake_bot.reply)
    assert "Berlin" in out
    assert "Sunny" in out


@pytest.mark.asyncio
async def test_weather_with_nick(fake_bot, fake_msg, patch_plugins,
                                 patch_aiohttp):
    # Use Bob for the target nick and London as location
    nicks = weather.JOINED_ROOMS["testroom@conference.example.com"]["nicks"]
    with patch.dict(nicks, {"Bob": {"jid": "bob@example.com"}}, clear=False), \
            patch.object(weather.vcard, "get_user_vcard",
                         AsyncMock(return_value={"LOCALITY": "London"})), \
            patch.object(weather.vcard, "vcard_field",
                         AsyncMock(return_value="London")):
        fake_msg["body"] = ",weather Bob"
        await weather.weather_command(fake_bot, "jid", "Alice", ["Bob"],
                                      fake_msg, True)
        fake_bot.reply.assert_called()
        out = output_of_reply(fake_bot.reply)
        # Should at least contain Bob or London somewhere!
        assert "London" in out or "Bob" in out


@pytest.mark.asyncio
async def test_weather_no_location(fake_bot, fake_msg, patch_plugins,
                                   patch_aiohttp):
    # .vcard_field returns {}
    with patch.object(weather.vcard, "get_user_vcard",
                      AsyncMock(return_value={})):
        await weather.weather_command(fake_bot, "jid", "Alice", [],
                                      fake_msg, True)
        fake_bot.reply.assert_called()
        out = output_of_reply(fake_bot.reply).lower()
        assert "no location" in out or "no location" in out.replace(" ", "")


@pytest.mark.asyncio
async def test_weather_api_fail(fake_bot, fake_msg, patch_plugins,
                                monkeypatch):
    """Simulate a 404/failure status"""
    class FailResp:
        status = 404

        async def text(self):
            return "Error from wttr"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class DummyAiohttpSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def get(self, *a, **k):
            return FailResp()

    monkeypatch.setattr(weather, "aiohttp", Mock(
        ClientSession=DummyAiohttpSession))
    with patch.object(weather.vcard, "get_user_vcard",
                      AsyncMock(return_value={"LOCALITY": "Berlin"})):
        await weather.weather_command(fake_bot, "jid", "Alice", [],
                                      fake_msg, True)
        fake_bot.reply.assert_called()
        out = output_of_reply(fake_bot.reply).lower()
        assert "failed" in out or "fetch" in out or "error" in out


@pytest.mark.asyncio
async def test_weather_keyerror_mucnick(fake_bot, fake_msg, patch_plugins,
                                        patch_aiohttp):
    # Simulate a msg lacking 'mucnick'
    msg = dict(fake_msg)
    msg.pop("mucnick", None)
    # Plugin should handle this gracefully, see plugin note below
    # (ideally plugin is patched to return a message about missing mucnick)
    with patch.object(weather.vcard, "get_user_vcard",
                      AsyncMock(return_value={"LOCALITY": "Berlin"})):
        await weather.weather_command(fake_bot, "jid", "Alice", [],
                                      msg, True)
        fake_bot.reply.assert_called()
        out = output_of_reply(fake_bot.reply).lower()
        # Should detect missing mucnick
        assert "berlin" in out


@pytest.mark.asyncio
async def test_weather_unicode_location(fake_bot, fake_msg, patch_plugins,
                                        patch_aiohttp):
    with patch.object(weather.vcard, "get_user_vcard",
                      AsyncMock(return_value={"LOCALITY":
                                              "München Hauptbahnhof"})), \
            patch.object(weather.vcard, "vcard_field",
                         AsyncMock(return_value="München Hauptbahnhof")), \
            patch.object(weather, "get_display_name",
                         AsyncMock(return_value="Alice")), \
            patch.object(weather, "aiohttp") as fake_aiohttp:
        # Provide a unicode-aware weather service
        class DummyResp:
            status = 200

            async def text(self):
                return "München Hauptbahnhof: Snow ❄️ -3°C"

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class DummySession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def get(self, *a, **k):
                return DummyResp()

        fake_aiohttp.ClientSession.return_value = DummySession()
        await weather.weather_command(fake_bot, "jid", "Alice", [],
                                      fake_msg, True)
        fake_bot.reply.assert_called()
        output = output_of_reply(fake_bot.reply)
        assert "münchen" in output.lower(
        ) or "hauptbahnhof" in output.lower() or "snow" in output.lower()
