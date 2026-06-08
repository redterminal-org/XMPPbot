import pytest
from unittest.mock import AsyncMock, MagicMock

import plugins.plugins as plugins_module


@pytest.fixture
def bot():
    """Mock a 'bot' object for plugin command handlers."""
    class DummyMsg(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.update(kwargs)
    m = MagicMock(name="bot")
    m.reply = MagicMock()
    m.prefix = ","
    m.version = "1.0.2"
    m.boundjid = "bot@example.org"
    m.get_user_role = AsyncMock(return_value=plugins_module.Role.ADMIN)
    m.bot_plugins = MagicMock()
    m.bot_plugins.list_detailed = AsyncMock(return_value={
        "core": {
            "loaded": ["plugins", "rooms"],
            "available": ["info"]
        },
        "fun": {
            "loaded": ["ducks"],
            "available": []
        }
    })
    m.bot_plugins.plugins = {
        "plugins": plugins_module,
        "rooms": MagicMock(),
        "info": MagicMock(),
    }
    m.bot_plugins.get_plugin_info = AsyncMock(
        side_effect=lambda name: {
            "plugins": {
                "name": "plugins",
                "version": "0.2.0",
                "category": "core",
                "description": "Runtime plugin management",
                "requires": [],
            },
            "info": {
                "name": "info",
                "version": "0.5.0",
                "category": "info",
                "description": "Information commands",
                "requires": ["core"],
            }
        }.get(name)
    )
    m.bot_plugins.load = AsyncMock()
    m.bot_plugins.load_all = AsyncMock()
    m.bot_plugins.unload = AsyncMock(return_value=(True, "Plugin unloaded."))
    m.bot_plugins.reload = AsyncMock(return_value=(True, "Plugin reloaded."))
    m.bot_plugins.list = MagicMock(return_value=["plugins", "rooms", "info"])
    return m


@pytest.fixture
def msg():
    """Mock message dict."""
    return {"from": MagicMock(), "type": "chat"}


@pytest.mark.asyncio
async def test_plugin_list(bot, msg):
    await plugins_module.plugin_list(bot, "adminjid", "AdminNick", [],
                                     msg, False)
    out = bot.reply.call_args[0][1]
    # Should mention all loaded and available plugins grouped by category
    assert "[CORE]" in out
    assert "[FUN]" in out
    assert "[loaded] plugins" in out
    assert "[not loaded] info" in out
    assert "[loaded] ducks" in out


@pytest.mark.asyncio
async def test_plugin_info_existing(bot, msg):
    await plugins_module.plugin_info(bot, "adminjid", "AdminNick",
                                     ["plugins"], msg, False)
    out = bot.reply.call_args[0][1]
    assert "Plugin: plugins" in out
    assert "Runtime plugin management" in out


@pytest.mark.asyncio
async def test_plugin_info_notfound(bot, msg):
    await plugins_module.plugin_info(bot, "adminjid", "AdminNick",
                                     ["notaplug"], msg, False)
    assert bot.reply.called
    # "Plugin 'notaplug' not found."
    assert "notaplug" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_info_missing_args(bot, msg):
    await plugins_module.plugin_info(bot, "adminjid", "AdminNick", [],
                                     msg, False)
    assert "Usage:" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_load_single_success(bot, msg):
    await plugins_module.plugin_load(bot, "adminjid", "AdminNick",
                                     ["info"], msg, False)
    # load() called
    bot.bot_plugins.load.assert_awaited_with("info")
    assert "Plugin 'info' loaded." in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_load_all(bot, msg):
    await plugins_module.plugin_load(bot, "adminjid", "AdminNick",
                                     ["all"], msg, False)
    bot.bot_plugins.load_all.assert_awaited()
    assert "All plugins loaded" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_load_error(bot, msg):
    bot.bot_plugins.load = AsyncMock(side_effect=Exception("fail"))
    await plugins_module.plugin_load(bot, "adminjid", "AdminNick",
                                     ["bad"], msg, False)
    assert "Error loading 'bad':" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_load_missing_args(bot, msg):
    await plugins_module.plugin_load(bot, "adminjid", "AdminNick", [],
                                     msg, False)
    assert "Usage:" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_unload_success(bot, msg):
    await plugins_module.plugin_unload(bot, "adminjid", "AdminNick",
                                       ["info"], msg, False)
    bot.bot_plugins.unload.assert_awaited_with("info", force=False)
    assert "Plugin unloaded" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_unload_force(bot, msg):
    await plugins_module.plugin_unload(bot, "adminjid", "AdminNick",
                                       ["info", "force"], msg, False)
    bot.bot_plugins.unload.assert_awaited_with("info", force=True)
    # Test that force argument is passed correctly


@pytest.mark.asyncio
async def test_plugin_unload_no_plugins(bot, msg):
    await plugins_module.plugin_unload(bot, "adminjid", "AdminNick",
                                       ["plugins"], msg, False)
    assert "Cannot unload plugin manager" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_unload_missing_args(bot, msg):
    await plugins_module.plugin_unload(bot, "adminjid", "AdminNick", [],
                                       msg, False)
    assert "Usage:" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_reload_single(bot, msg):
    await plugins_module.plugin_reload(bot, "adminjid", "AdminNick",
                                       ["info"], msg, False)
    bot.bot_plugins.reload.assert_awaited_with("info", auto=False)
    assert "reloaded" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_reload_auto_flag(bot, msg):
    await plugins_module.plugin_reload(bot, "adminjid", "AdminNick",
                                       ["info", "auto"], msg, False)
    bot.bot_plugins.reload.assert_awaited_with("info", auto=True)
    assert "reloaded" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_reload_missing_args(bot, msg):
    await plugins_module.plugin_reload(bot, "adminjid", "AdminNick", [],
                                       msg, False)
    assert "Usage:" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_reload_all_success(bot, msg):
    bot.bot_plugins.list = MagicMock(return_value=["plugins", "rooms"])
    bot.bot_plugins.reload = AsyncMock(return_value=(True, "Reloaded"))
    await plugins_module.plugin_reload(bot, "adminjid", "AdminNick",
                                       ["all"], msg, False)
    assert "plugins" in bot.bot_plugins.reload.call_args[
        0] or bot.bot_plugins.reload.call_args_list[0]
    assert "reloaded successfully" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_plugin_reload_all_auto_some_errors(bot, msg):
    bot.bot_plugins.list = MagicMock(return_value=["plugins", "rooms", "info"])
    # First two succeed, info fails

    def reload_side(name, auto):
        if name == "info":
            return (False, "Failed")
        return (True, "Reloaded")
    bot.bot_plugins.reload = AsyncMock(side_effect=reload_side)
    await plugins_module.plugin_reload(bot, "adminjid", "AdminNick",
                                       ["all", "auto"], msg, False)
    out = bot.reply.call_args[0][1]
    assert "some errors" in out or "errors" in out
    assert "- info:" in out
