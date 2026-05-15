import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, MagicMock, patch

import plugins.presence as presence

@pytest.fixture
def fake_bot():
    # Minimal bot mock with expected attributes/methods
    bot = Mock()
    bot.prefix = "!"
    bot.db = Mock()
    bot.db.users.plugin = AsyncMock(return_value=Mock())
    bot.reply = Mock()
    bot.get_user_role = AsyncMock(return_value=0)  # 0 for admin
    bot.presence = Mock()
    bot.presence.status = {"show": "online", "status": "All systems go"}
    bot.presence.emoji = lambda show: "😀"
    bot.presence.update = Mock()
    return bot

@pytest.fixture
def fake_msg():
    bare_jid = "room@conf.example.com"
    msg = {
        "type": "groupchat",
        "from": MagicMock(),
        "to": MagicMock(),
        "reply": None,
    }
    msg["from"].bare = bare_jid
    msg["from"].resource = "Tester"
    msg["to"].bare = "bot@example.com"
    return msg

@pytest.mark.asyncio
async def test_presence_show_normal_room(fake_bot, fake_msg, monkeypatch):
    # enabled room, is_room True, bot replies with status
    fake_enabled_rooms = {fake_msg["from"].bare: True}

    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: False)
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value=fake_enabled_rooms))

    await presence.presence_show(fake_bot, "user@example.com", "Tester", [], fake_msg, is_room=True)

    fake_bot.reply.assert_called_once()
    args = fake_bot.reply.call_args[0]
    assert "Current status" in args[1]
    assert "(online)" in args[1]

@pytest.mark.asyncio
async def test_presence_show_disabled_room(fake_bot, fake_msg, monkeypatch):
    # room not enabled, should get disabled message
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: False)
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value={}))

    await presence.presence_show(fake_bot, "user@example.com", "Tester", [], fake_msg, is_room=True)

    fake_bot.reply.assert_called_once()
    text = fake_bot.reply.call_args[0][1]
    assert "disabled in this room" in text

@pytest.mark.asyncio
async def test_presence_show_muc_pm_toggle_invokes_handle_toggle(fake_bot, fake_msg, monkeypatch):
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: True)
    # Make handle_room_toggle_command return True (meaning handled)
    monkeypatch.setattr(presence, "handle_room_toggle_command", AsyncMock(return_value=True))

    await presence.presence_show(fake_bot, "user@example.com", "Tester", ["on"], fake_msg, is_room=False)
    presence.handle_room_toggle_command.assert_awaited_once()

@pytest.mark.asyncio
async def test_presence_show_muc_pm_toggle_not_handled(fake_bot, fake_msg, monkeypatch):
    # Toggle not handled, should proceed to show presence
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: True)
    monkeypatch.setattr(presence, "handle_room_toggle_command", AsyncMock(return_value=False))
    fake_enabled_rooms = {fake_msg["from"].bare: True}
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value=fake_enabled_rooms))

    await presence.presence_show(fake_bot, "user@example.com", "Tester", ['status'], fake_msg, is_room=False)
    fake_bot.reply.assert_called()
    args = fake_bot.reply.call_args[0]
    assert "Current status" in args[1]

@pytest.mark.asyncio
async def test_presence_set_valid_states(fake_bot, fake_msg, monkeypatch, caplog):
    # enabled room, valid states, with and without a message
    fake_enabled_rooms = {fake_msg["from"].bare: True}
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value=fake_enabled_rooms))
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: False)

    # With message
    await presence.presence_set(fake_bot, "admin@example.com", "admin", ["away", "Lunch"], fake_msg, is_room=True)
    fake_bot.presence.update.assert_called_with("away", "Lunch")
    fake_bot.reply.assert_called()
    assert "(away)" in fake_bot.reply.call_args[0][1]
    assert "Lunch" in fake_bot.reply.call_args[0][1]

    # Without message
    fake_bot.reply.reset_mock()
    await presence.presence_set(fake_bot, "admin@example.com", "admin", ["dnd"], fake_msg, is_room=True)
    fake_bot.presence.update.assert_called_with("dnd", "")
    assert "(dnd)" in fake_bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_presence_set_invalid_state(fake_bot, fake_msg, monkeypatch):
    fake_enabled_rooms = {fake_msg["from"].bare: True}
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value=fake_enabled_rooms))
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: False)

    await presence.presence_set(fake_bot, "admin@example.com", "admin", ["sleeping"], fake_msg, is_room=True)
    fake_bot.reply.assert_called_once()
    assert "Invalid status" in fake_bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_presence_set_missing_args(fake_bot, fake_msg, monkeypatch):
    fake_enabled_rooms = {fake_msg["from"].bare: True}
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value=fake_enabled_rooms))
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: False)

    await presence.presence_set(fake_bot, "admin@example.com", "admin", [], fake_msg, is_room=True)
    fake_bot.reply.assert_called_once()
    assert "Usage: " in fake_bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_presence_set_disabled(fake_bot, fake_msg, monkeypatch):
    # Not enabled, should block action
    monkeypatch.setattr(presence, "_get_enabled_rooms", AsyncMock(return_value={}))
    monkeypatch.setattr(presence, "_is_muc_pm", lambda msg: False)

    await presence.presence_set(fake_bot, "admin@example.com", "admin", ["online"], fake_msg, is_room=True)
    fake_bot.reply.assert_called_once()
    assert "disabled in this room" in fake_bot.reply.call_args[0][1]

@pytest.mark.asyncio
async def test_get_presence_store(fake_bot):
    # Verifies it proxies to db.users.plugin
    plugin_store = await presence.get_presence_store(fake_bot)
    fake_bot.db.users.plugin.assert_awaited_with("presence")

