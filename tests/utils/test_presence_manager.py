import pytest
from utils.presence_manager import PresenceManager


class DummyBot:
    def __init__(self):
        self.calls = []
        self.bot_plugins = type("Plugins", (), {"plugins": {}})()
    def send_presence(self, **kwargs):
        self.calls.append(kwargs)


def test_presence_manager_update_sets_status():
    bot = DummyBot()
    pm = PresenceManager(bot)
    pm.update("chat", "Chatting!")
    assert pm.status["show"] == "chat"
    assert pm.status["status"] == "Chatting!"


def test_presence_manager_emoji_for_states():
    bot = DummyBot()
    pm = PresenceManager(bot)
    # All known
    assert pm.emoji("online") == "✅"
    assert pm.emoji("chat") == "💬"
    assert pm.emoji("xa") == "💤"
    assert pm.emoji("dnd") == "⛔"
    assert pm.emoji("away") == "👋 "
    # Fallback
    assert pm.emoji("notreal") == ""


def test_broadcast_sends_presence(monkeypatch):
    bot = DummyBot()
    pm = PresenceManager(bot)
    # Simulate no rooms plugin
    pm.broadcast()
    assert len(bot.calls) == 1
    assert "pshow" not in bot.calls[0] or isinstance(bot.calls[0]["pshow"], str)


def test_broadcast_with_rooms(monkeypatch):
    bot = DummyBot()
    pm = PresenceManager(bot)
    room_plugin = type("Rooms", (), {"JOINED_ROOMS": {
        "room1": {"nick": "Bob"},
        "room2": {"nick": None}
    }})()
    bot.bot_plugins.plugins["rooms"] = room_plugin
    pm.broadcast()
    # Should call send_presence for bot plus one extra for room1 (has nick)
    assert len(bot.calls) == 2
    main, room = bot.calls
    assert "pto" not in main
    assert room["pto"] == "room1/Bob"
