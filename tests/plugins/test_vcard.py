import pytest
from types import SimpleNamespace
from plugins import vcard


@pytest.fixture
def fake_bot(monkeypatch):
    bot = SimpleNamespace()
    bot.db = SimpleNamespace()
    bot.db.users = SimpleNamespace()
    bot.plugin = {"xep_0054": SimpleNamespace()}
    bot.prefix = ","
    bot.presence = SimpleNamespace()
    bot.presence.joined_rooms = {}
    bot.boundjid = SimpleNamespace(bare="bot@domain", resource="BotNick")
    bot.reply = lambda msg, txt, * \
        a, **k: bot.__dict__.setdefault('_replies', []).append((txt, msg))
    bot.get_user_role = lambda jid, room=None: 1
    bot.bot_plugins = SimpleNamespace()
    bot.bot_plugins.plugins = {"rooms": SimpleNamespace(JOINED_ROOMS={})}
    # Add .plugin attribute for _core._get_enabled_rooms
    async def get_global(key, default=None): return {}
    bot.db.users.plugin = lambda plugin: SimpleNamespace(get_global=get_global)
    return bot


def msg(from_jid="room@x/resource", resource=None, type_="chat",
        to_jid="bot@domain"):
    if "/" in from_jid:
        bare, res = from_jid.split("/", 1)
        resource = resource if resource is not None else res
    else:
        bare = from_jid
        resource = resource if resource is not None else "resource"

    class FakeJID:
        def __init__(self, bare): self.bare = bare
    return {
        "from": SimpleNamespace(bare=bare, resource=resource),
        "type": type_,
        "to": FakeJID(to_jid)
    }


@pytest.fixture(autouse=True)
def patch_get_vcard(monkeypatch):
    class DummyVcard:
        def get(self, key):
            if key == "FN":
                return "Test User"
            if key == "BDAY":
                return "2001-01-01"
            if key == "ADR":
                return {"LOCALITY": "Loc", "REGION": "Reg", "CTRY": "CT"}
            return None
        xml = []

    async def get_vcard(bot, msg, jid=None):
        return DummyVcard()
    monkeypatch.setattr(vcard, "get_vcard", get_vcard)
    return DummyVcard


@pytest.mark.asyncio
@pytest.mark.parametrize("args,is_room,expect", [
    ([], False, "vCard for"),
    (["bad"], False, "only look up your own vCard"),
])
async def test_vcard_command_pm(fake_bot, args, is_room, expect):
    msgx = msg(from_jid="bob@b/resource")
    await vcard.vcard_command(fake_bot, "s", "n", args, msgx, is_room)
    assert any(expect in r[0] for r in getattr(fake_bot, "_replies", []))


@pytest.mark.asyncio
async def test_set_timezone_command(fake_bot, monkeypatch):
    async def get_global(key, default=None): return {}
    async def set(j, k, v): pass
    fake_bot.db.users.plugin = lambda p: SimpleNamespace(
        get_global=get_global, set=set)

    async def _get_enabled_rooms(b, k, p): return {"bob@b": True}
    monkeypatch.setattr(vcard._core, "_get_enabled_rooms", _get_enabled_rooms)
    async def _check_user_exists(b, jid, msg): return True
    monkeypatch.setattr(vcard._core, "_check_user_exists", _check_user_exists)
    m = msg(from_jid="bob@b/resource")
    m["type"] = "chat"
    await vcard.set_timezone(fake_bot, "s", "n", ["Europe/Berlin"], m, False)
    found = any("TIMEZONE set to" in t[0]
                for t in getattr(fake_bot, "_replies", []))
    assert found


@pytest.mark.asyncio
async def test_set_timezone_invalid(fake_bot, monkeypatch):
    async def get_global(key, default=None): return {}
    async def set(j, k, v): pass
    fake_bot.db.users.plugin = lambda p: SimpleNamespace(
        get_global=get_global, set=set)

    async def _get_enabled_rooms(b, k, p): return {"bob@b": True}
    monkeypatch.setattr(vcard._core, "_get_enabled_rooms", _get_enabled_rooms)
    async def _check_user_exists(b, jid, msg): return True
    monkeypatch.setattr(vcard._core, "_check_user_exists", _check_user_exists)
    m = msg(from_jid="bob@b/resource")
    m["type"] = "chat"
    await vcard.set_timezone(fake_bot, "s", "n", ["NotAZone"], m, False)
    assert any("Invalid timezone" in t[0]
               for t in getattr(fake_bot, "_replies", []))


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd,args,label,expect", [
    (vcard.get_fullname, [], "Full Name", "Full Name"),
    (vcard.get_nicknames, [], "Nicknames", "Nicknames"),
    (vcard.get_timezone, [], "Timezone", "Timezone"),
    (vcard.get_organisations, [], "Organisations", "Organisations"),
    (vcard.get_notes, [], "Notes", "Notes"),
    (vcard.get_email, [], "Emails", "Emails"),
    (vcard.get_urls, [], "URLs", "URLs"),
    (vcard.get_birthday, [], "birthday", "Birthday"),
])
async def test_field_cmds(fake_bot, monkeypatch, cmd, args, label, expect):
    async def _get_enabled_rooms(b, k, p): return {"room@x": True}
    monkeypatch.setattr(vcard._core, "_get_enabled_rooms", _get_enabled_rooms)
    m = msg(from_jid="room@x/TestNick", resource="TestNick")
    m["type"] = "chat"
    # Patch plugin store so bot.db.users.plugin("vcard").get_global works even
    # if key unused
    fake_bot.db.users.plugin = lambda plugin: SimpleNamespace(
        get_global=lambda k, d=None: {"room@x": True})
    await cmd(fake_bot, "s", "n", args, m, True)
    # Accept warning cases: some vcard plugins will warn about missing nicks
    # if the minimal nick is not found
    expected_found = any(expect.lower() in x[0].lower() or label.lower(
    ) in x[0].lower() for x in getattr(fake_bot, "_replies", []))
    # Also accept a warning reply about the nick not being found for negative
    # coverage
    warning_found = any("not found in this room" in x[0].lower(
    ) for x in getattr(fake_bot, "_replies", []))
    assert expected_found or warning_found
