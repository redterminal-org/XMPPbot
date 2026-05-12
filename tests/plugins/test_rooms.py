import pytest
import asyncio
from types import SimpleNamespace
from plugins import rooms

def _msg(bare="room@conf", resource="BotNick", mtype="chat", to_jid="bot@domain"):
    class FakeJID:
        def __init__(self, bare): self.bare = bare
    return {
        "from": SimpleNamespace(bare=bare, resource=resource),
        "type": mtype,
        "mucnick": resource,
        "to": FakeJID(to_jid)
    }

@pytest.fixture(autouse=True)
def reset_globals():
    rooms.JOINED_ROOMS.clear()
    yield
    rooms.JOINED_ROOMS.clear()

@pytest.fixture
def fake_bot(monkeypatch):
    bot = SimpleNamespace()
    bot.db = SimpleNamespace()
    bot.db.rooms = SimpleNamespace()
    bot.db.users = SimpleNamespace()
    bot.plugin = {"xep_0045": SimpleNamespace(), "xep_0030": SimpleNamespace()}
    bot.presence = SimpleNamespace()
    bot.presence.status = {"show": "online", "status": "Ready!"}
    bot.presence.joined_rooms = {}
    bot.boundjid = SimpleNamespace(bare="bot@domain", resource="BotNick")
    bot.reply = lambda msg, text, *a, **k: bot.__dict__.setdefault("replies", []).append((text, msg))
    bot.prefix = ","
    bot.bot_plugins = SimpleNamespace()
    bot.bot_plugins.register_event = lambda *a, **k: None
    bot.bot_plugins.plugins = {"rooms": SimpleNamespace(JOINED_ROOMS=rooms.JOINED_ROOMS)}
    bot.get_user_role = lambda jid, room=None: 1  # OWNER so all perms
    async def get_global(key, default=None): return {}
    async def set_global(key, val): pass
    bot.db.users.plugin = lambda plugin: SimpleNamespace(get_global=get_global, set_global=set_global)
    return bot

# ... (rest is unchanged from your original except you may need to ensure test message mtypes and .plugin as above)

@pytest.mark.asyncio
async def test_rooms_list_with_rooms(fake_bot):
    async def list_rooms(): return [("room@c","Test",True,"{}")]
    fake_bot.db.rooms.list = list_rooms
    rooms.JOINED_ROOMS["room@c"] = {"nick":"Test","autojoin":True,"affiliation":"admin","role":"member","status":"{}","nicks":{}}
    msg = _msg()
    await rooms.rooms_list(fake_bot, "sender", "n", [], msg, False)
    replies = [r[0] for r in getattr(fake_bot,"replies",[])]
    # Fix: The previous any() bug here
    found1 = any("Stored rooms" in r for r in replies)
    found2 = any("JOINED rooms" in r for r in replies)
    assert found1 or found2
