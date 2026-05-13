import asyncio
import datetime
import logging

import pytest

import plugins.birthday_notify as birthday_notify

from types import SimpleNamespace

# --- Mock helpers

class DummyStore:
    def __init__(self):
        self.data = {}

    async def get(self, jid, key, default=None):
        d = self.data.setdefault(jid, {})
        return d.get(key, default)

    async def set(self, jid, key, value):
        self.data.setdefault(jid, {})[key] = value

class DummyUsers:
    def __init__(self):
        self._stores = {}  # persistent store per plugin name

    async def flush_all(self):
        return

    def plugin(self, name):
        if name not in self._stores:
            self._stores[name] = DummyStore()
        return self._stores[name]

class DummyBot:
    def __init__(self):
        self._sent = []
        self.db = SimpleNamespace(users=DummyUsers())
        self.boundjid = SimpleNamespace(bare="bot@xmpp")
        self.replies = []
        self._called = []
        self.bot_plugins = SimpleNamespace(register_event=self._register_event)
    def make_message(self, **kwargs):
        msg = SimpleNamespace(**kwargs)
        msg.send = lambda : self._sent.append(msg)
        return msg
    async def _safe_send_message(self, msg):
        self._sent.append(msg)
    def reply(self, msg, text, *a, **k):
        self.replies.append((text, msg))
    def _register_event(self, *a, **k):
        self._called.append(("register_event", a, k))

# --- Fixtures

@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    # Reset all global state between tests, including JOINED_ROOMS and ANNOUNCED_TODAY
    backup = dict(birthday_notify.ANNOUNCED_TODAY)
    birthday_notify.ANNOUNCED_TODAY.clear()
    birthday_notify.JOINED_ROOMS.clear()
    if hasattr(birthday_notify, "_BIRTHDAY_CHECK_TASK"):
        birthday_notify._BIRTHDAY_CHECK_TASK = None
    yield
    birthday_notify.ANNOUNCED_TODAY.clear()
    birthday_notify.JOINED_ROOMS.clear()
    for k, v in backup.items():
        birthday_notify.ANNOUNCED_TODAY[k] = v

@pytest.fixture
def bot(monkeypatch):
    # Patch get_profile to always return a fixed birthday unless set per test
    default_bday = "1995-05-13"
    async def get_profile(bot_instance, msg, jid):
        # Optionally add ._test_vcard_bday to bot to override
        b = getattr(bot_instance, "_test_vcard_bday", default_bday)
        return {'BDAY': b}
    monkeypatch.setattr(birthday_notify, "get_profile", get_profile)
    # Patch handle_room_toggle_command to no-op (simulate always False)
    async def handle_room_toggle_command(*a, **k): return False
    monkeypatch.setattr(birthday_notify, "handle_room_toggle_command", handle_room_toggle_command)
    # Patch _is_enabled_for_room to True
    async def always_enabled(*a, **k): return True
    monkeypatch.setattr(birthday_notify, "_is_enabled_for_room", always_enabled)
    # Patch _ensure_user_exists to no-op
    async def noop(*a, **k): return None
    monkeypatch.setattr(birthday_notify, "_ensure_user_exists", noop)
    yield DummyBot()

# --- Tests ---

def test_parse_birthday_formats():
    # Full date
    pd = birthday_notify._parse_birthday("1980-12-31")
    assert pd["year"] == 1980 and pd["month"] == 12 and pd["day"] == 31
    pd = birthday_notify._parse_birthday("19801231")
    assert pd["year"] == 1980 and pd["month"] == 12 and pd["day"] == 31
    # No year - MM-DD
    pd = birthday_notify._parse_birthday("11-05")
    assert pd["year"] is None and pd["month"] == 11 and pd["day"] == 5
    pd = birthday_notify._parse_birthday("1105")
    assert pd["year"] is None and pd["month"] == 11 and pd["day"] == 5
    pd = birthday_notify._parse_birthday("--11-05")
    assert pd["year"] is None and pd["month"] == 11 and pd["day"] == 5
    # Leap day
    pd = birthday_notify._parse_birthday("--02-29")
    assert pd and pd["day"] == 29
    # Invalid
    pd = birthday_notify._parse_birthday("2026-02-30")
    assert pd is None
    pd = birthday_notify._parse_birthday("bad-data")
    assert pd is None

def test_normalize_bday_value():
    assert birthday_notify._normalize_bday_value("1990-01-01") == "1990-01-01"
    assert birthday_notify._normalize_bday_value(["", "1991-02-03"]) == "1991-02-03"
    assert birthday_notify._normalize_bday_value(["none", None]) is None
    assert birthday_notify._normalize_bday_value("null") is None
    assert birthday_notify._normalize_bday_value("-") is None
    assert birthday_notify._normalize_bday_value(None) is None

def test_is_birthday_today(monkeypatch):
    today = datetime.date.today()
    # Today (with year)
    d = f"{today.year}-{today.month:02}-{today.day:02}"
    assert birthday_notify._is_birthday_today(d)
    # Today (no year)
    d2 = f"{today.month:02}-{today.day:02}"
    assert birthday_notify._is_birthday_today(d2)
    # Not today
    other = (today.month % 12 + 1, (today.day % 28) + 1)
    not_today = f"{other[0]:02}-{other[1]:02}"
    assert not birthday_notify._is_birthday_today(not_today)

def test_calculate_age(monkeypatch):
    today = datetime.date.today()
    # Birthday already occurred this year
    d = f"{today.year-10}-{today.month:02}-{1 if today.day>1 else 2:02}"
    assert birthday_notify._calculate_age(d) == 10 if today.day>1 else 9
    # Birthday upcoming this year
    month = today.month
    day = today.day+1 if today.day < 28 else 1
    year = today.year-20
    d = f"{year}-{month:02}-{day:02}"
    expected = 19 if (month, day) > (today.month, today.day) else 20
    assert birthday_notify._calculate_age(d) in (19,20)
    # No year returns None
    assert birthday_notify._calculate_age(f"{today.month:02}-{today.day:02}") is None

@pytest.mark.asyncio
async def test_check_user_birthday_announce(monkeypatch, bot):
    # Today is bot's _test_vcard_bday, always triggers
    today = datetime.date.today()
    returned_bday = f"{today.year}-{today.month:02}-{today.day:02}"
    async def _get_birthday_cached_or_live(botarg, room, user_jid, nick):
        return returned_bday
    monkeypatch.setattr(birthday_notify, "_get_birthday_cached_or_live", _get_birthday_cached_or_live)
    # So is_birthday_today returns True, path with age: triggers announcement
    await birthday_notify._check_user_birthday(bot, "user@x", "Nick", "room@room")
    # Should have sent a message
    sent_msgs = bot._sent
    assert any("Happy Birthday" in getattr(m, "mbody", "") for m in sent_msgs)

@pytest.mark.asyncio
async def test_check_user_birthday_not_today(monkeypatch, bot):
    async def _get_birthday_cached_or_live(bot, r, u, n): return "2000-01-01"
    monkeypatch.setattr(birthday_notify, "_get_birthday_cached_or_live", _get_birthday_cached_or_live)
    # Birthday is not today, no announcement
    await birthday_notify._check_user_birthday(bot, "user@x", "Nick", "r@r")
    assert not bot._sent

@pytest.mark.asyncio
async def test_check_user_birthday_no_birthday(monkeypatch, bot):
    async def _get_birthday_cached_or_live(bot, r, u, n): return None
    monkeypatch.setattr(birthday_notify, "_get_birthday_cached_or_live", _get_birthday_cached_or_live)
    await birthday_notify._check_user_birthday(bot, "user@x", "Nick", "room@r")
    assert not bot._sent

@pytest.mark.asyncio
async def test_check_user_birthday_exception(monkeypatch, bot, caplog):
    # Force _get_birthday_cached_or_live to raise
    async def raiseit(bot, a, b, c): raise Exception("fail!")
    monkeypatch.setattr(birthday_notify, "_get_birthday_cached_or_live", raiseit)
    with caplog.at_level(logging.ERROR):
        await birthday_notify._check_user_birthday(bot, "u@x", "Nick", "r@r")
    assert any("Error checking user birthday:" in r.message for r in caplog.records)

@pytest.mark.asyncio
async def test_check_room_birthdays(monkeypatch, bot):
    # Room enabled
    # Fill up JOINED_ROOMS with users, as the plugin expects
    room = "room1@conf"
    birthday_notify.JOINED_ROOMS[room] = {"nicks":{
        "NickA":{"jid":"jidA"},
        "NickB":{"jid":"jidB"},
        123:None, # Should skip non-dict entries
    }}
    called = []
    async def check_user(bot, user_jid, nick, room_jid):
        called.append((user_jid, nick, room_jid))
    monkeypatch.setattr(birthday_notify, "_check_user_birthday", check_user)
    await birthday_notify._check_room_birthdays(bot, room)
    assert ("jidA", "NickA", room) in called
    assert ("jidB", "NickB", room) in called

@pytest.mark.asyncio
async def test_check_and_announce_birthdays(monkeypatch, bot):
    checked = []
    rooms = ["roomA@conf", "roomB@conf"]
    for r in rooms:
        birthday_notify.JOINED_ROOMS[r] = {"nicks": {}}
    async def _check_room_birthdays(bot, room_jid):
        checked.append(room_jid)
    monkeypatch.setattr(birthday_notify, "_check_room_birthdays", _check_room_birthdays)
    await birthday_notify._check_and_announce_birthdays(bot)
    assert set(checked) == set(rooms)

@pytest.mark.asyncio
async def test_birthday_notify_command_usage(bot):
    # Always returns False, so sends usage text
    msg = {"from": SimpleNamespace(bare="roomA@conf"), "type": "groupchat"}
    await birthday_notify.birthday_notify_command(bot, "s", "n", [], msg, True)
    assert bot.replies and "Usage" in bot.replies[-1][0]

@pytest.mark.asyncio
async def test_birthday_notify_command_enabled_checks_room(monkeypatch, bot):
    # Test hint branch: toggles to "on" and is DM but bare in JOINED_ROOMS
    msg = {"from": SimpleNamespace(bare="roomZ@conf"), "type": "chat"}
    async def dummy_handle(*a, **k): return True
    monkeypatch.setattr(birthday_notify, "handle_room_toggle_command", dummy_handle)
    # Should schedule _check_room_birthdays
    birthday_notify.JOINED_ROOMS["roomZ@conf"] = {"nicks": {}}
    called = {}
    async def fake_check(bot, room_jid):
        called[room_jid] = True
    monkeypatch.setattr(birthday_notify, "_check_room_birthdays", fake_check)
    # Patch asyncio.create_task for fire and forget
    tasks = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: asyncio.ensure_future(coro))
    await birthday_notify.birthday_notify_command(bot, "s", "n", ["on"], msg, False)
    await asyncio.sleep(0)
    assert "roomZ@conf" in called

@pytest.mark.asyncio
async def test_birthday_check_loop_lifecycle(monkeypatch, bot):
    # Test the on_ready/on_load/on_unload hooks: makes a background task.
    called = []

    monkeypatch.setattr(asyncio, "create_task", lambda coro: asyncio.ensure_future(coro))
    await birthday_notify.on_ready(bot)
    await birthday_notify.on_load(bot)
    await birthday_notify.on_unload(bot)
    # No lingering coroutine warnings now!

@pytest.mark.asyncio
async def test_birthday_cache(monkeypatch, bot):
    # test _set_cached_bday and _get_cached_bday
    val = "2000-02-02"
    u = bot.db.users
    await birthday_notify._set_cached_bday(bot, "jid1", val, nick="John")
    outval, updated = await birthday_notify._get_cached_bday(bot, "jid1")
    assert outval == "2000-02-02"
    # Negative cache
    await birthday_notify._set_cached_bday(bot, "jid2", None)
    outval2, updated2 = await birthday_notify._get_cached_bday(bot, "jid2")
    assert outval2 is None

@pytest.mark.asyncio
async def test_get_birthday_from_vcard(monkeypatch, bot):
    # get_profile patched by the bot fixture
    out = await birthday_notify._get_birthday_from_vcard(bot, "room@conf", "Nick")
    assert out == (True, "1995-05-13")
    # Simulate vCard error
    monkeypatch.setattr(birthday_notify, "get_profile", lambda *a, **k: (_ for _ in ()).throw(Exception("fail!")))
    out2 = await birthday_notify._get_birthday_from_vcard(bot, "room@conf", "Nick")
    assert out2 == (False, None)

@pytest.mark.asyncio
async def test_get_birthday_cached_or_live(monkeypatch, bot):
    # Should use cache, or fallback to vcard, or negative cache
    calls = {"vcard": 0}
    async def fake_vcard(bot, room, nick): calls["vcard"]+=1; return (True, "1960-12-30")
    monkeypatch.setattr(birthday_notify, "_get_birthday_from_vcard", fake_vcard)
    # No cache, uses vcard and caches it
    result = await birthday_notify._get_birthday_cached_or_live(bot, "room@conf", "jid3", "Nick3")
    assert result == "1960-12-30"
    # Now force cache to exist and be fresh (simulate db state)
    await birthday_notify._set_cached_bday(bot, "jid3", "1970-01-02")
    r, ts = await birthday_notify._get_cached_bday(bot, "jid3")
    monkeypatch.setattr(birthday_notify, "_now_ts", lambda: ts)
    result2 = await birthday_notify._get_birthday_cached_or_live(bot, "room@conf", "jid3", "Nick3")
    assert result2 == "1970-01-02"
    # Negative cache skips live
    await birthday_notify._set_cached_bday(bot, "jid4", None)
    monkeypatch.setattr(birthday_notify, "_now_ts", lambda: ts)
    result3 = await birthday_notify._get_birthday_cached_or_live(bot, "room@conf", "jid4", "Nick4")
    assert result3 is None

@pytest.mark.asyncio
async def test_load_and_mark_announced(bot):
    d = datetime.date.today().isoformat()
    # No prior announcement
    assert await birthday_notify._load_announced_date(bot, "roomA", "jidA") is None
    await birthday_notify._mark_announced(bot, "roomA", "jidA", d)
    assert await birthday_notify._load_announced_date(bot, "roomA", "jidA") == d
    # Should also cache in ANNOUNCED_TODAY
    assert birthday_notify.ANNOUNCED_TODAY[("roomA","jidA")] == d
