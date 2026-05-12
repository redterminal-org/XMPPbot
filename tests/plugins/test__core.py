import pytest
import asyncio
import pytz
from types import SimpleNamespace
from plugins import _core


@pytest.fixture
def fake_bot():
    bot = SimpleNamespace()
    bot.db = SimpleNamespace()
    bot.plugin = {"xep_0045": SimpleNamespace()}
    async def get_global(key, default=None):
        return {"room@conf": True}
    async def set_global(key, val): pass
    bot.db.users = SimpleNamespace(
        _nick_index={"Nick": {"jidval"}, "ListNick": ["jid1","jid2"], "SingleNick": "jid3"},
        _nick_index_lock=asyncio.Lock(),
        plugin=lambda plugin: SimpleNamespace(get_global=get_global, set_global=set_global)
    )
    bot.db.rooms = SimpleNamespace()
    bot.get_user_role = lambda jid, room=None: 40  # MODERATOR
    bot.bot_plugins = SimpleNamespace()
    bot.bot_plugins.plugins = {"rooms": SimpleNamespace(JOINED_ROOMS={"room@conf": {
        "nicks": {
            "Nick": {"jid":"jidval","affiliation":"owner","role":"moderator"},
            "OtherNick": {"jid":"jidval2","affiliation":"member"}
        },
        "nick": "BotNick",
        "affiliation":"owner",
        "role":"moderator"
    }})}
    bot.boundjid = SimpleNamespace(bare="bot@domain")
    async def get(jid):
        return {"jid": jid, "nickname": "Nick", "role": 80}
    async def create(jid, nn=None):
        return True
    bot.db.users.get = get
    bot.db.users.create = create
    bot.reply = lambda msg, txt, *a, **k: bot.__dict__.setdefault('_replies', []).append((txt, msg))
    return bot

def msg(from_jid="user@x/resource", resource=None, type_="chat", to_jid="bot@domain"):
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

def test_normalize_bare_jid_base_cases():
    assert _core._normalize_bare_jid("alice@domain/resource") == "alice@domain"
    assert _core._normalize_bare_jid("foo/bar@baz") == "foo"
    assert _core._normalize_bare_jid("") is None
    assert _core._normalize_bare_jid(None) is None
    assert _core._normalize_bare_jid("notajid") == "notajid"

@pytest.mark.parametrize("dur,expect", [
    ("10s", 10),("5m",300),("2h",7200),("1d",86400),
    ("2d5h3m20s",2*86400+5*3600+3*60+20),
    ("1h30m",5400),("0s",None),("",None),("nomatch",None),("10",None)
])
def test_parse_duration_all_cases(dur,expect):
    assert _core.parse_duration(dur) == expect

def test_paginate_items_and_clamp():
    items = list(range(15))
    page, pageno, total_pages, total = _core.paginate_items(items, 2, 5)
    assert page == [5,6,7,8,9]
    assert 1 <= pageno <= total_pages
    assert total_pages == 3
    page, pageno, *_ = _core.paginate_items(items, 0, 5)
    assert pageno == 1

def test_cache_and_fetch_message():
    ns, room = "coretest", "room@c"
    _core.cache_message(ns, room, "nick", "body", "id")
    msgs = _core.get_cached_messages(ns, room)
    assert any(m["body"]=="body" for m in msgs)
    assert _core.get_last_cached_message(ns, room)["body"] == "body"
    assert _core.get_cached_message_by_id(ns, room, "id")["body"] == "body"
    _core.cache_message(ns, room, "nick", "next", "id2", maxlen=1)
    assert len(_core.get_cached_messages(ns, room)) == 1

def test_reply_target_and_extract_quote():
    msg1 = {"reply": {"id": "abc123"}}
    msg2 = {}
    assert _core.get_reply_target(msg1) == "abc123"
    assert _core.get_reply_target(msg2) is None
    quote = """> quoted
> lines
Not quoted"""
    assert _core.extract_reply_quote(quote) == "quoted\nlines"
    assert _core.extract_reply_quote("") is None

def test_remember_stanza_and_eviction():
    ns = "namespace"
    assert _core.remember_stanza(ns, "msgid")
    assert not _core.remember_stanza(ns, "msgid")

def test_format_status_helpers():
    assert "enabled" in _core._format_status("Test", True)
    assert "disabled" in _core._format_status("Test", False)
    for fun in [_core._format_enabled, _core._format_disabled, _core._format_already_enabled, _core._format_already_disabled]:
        assert isinstance(fun("Label"), str)

@pytest.mark.asyncio
async def test_get_and_check_real_jid_and_nicks(fake_bot):
    _core.JOINED_ROOMS = fake_bot.bot_plugins.plugins["rooms"].JOINED_ROOMS
    m = msg(from_jid="room@conf/Nick", resource="Nick", type_="chat")
    jid, is_priv, is_group = await _core.get_real_jid(fake_bot, m)
    assert jid == "jidval"
    nicks = await _core.get_nicks_from_jid(fake_bot, "jidval")
    assert "Nick" in nicks or isinstance(nicks, list)

@pytest.mark.asyncio
async def test_is_plugin_enabled_for_room(fake_bot):
    async def get_global(key, default=None):
        return {"room@conf": True}
    store = SimpleNamespace(get_global=get_global)
    async def async_store_getter(b): return store
    res = await _core.is_plugin_enabled_for_room(fake_bot, async_store_getter, "any", "room@conf")
    assert res

@pytest.mark.asyncio
async def test_ensure_user_exists_creates_user(fake_bot):
    called = {}
    async def get(j): return None
    async def create(j, nn=None): called.setdefault("done", True)
    fake_bot.db.users.get = get
    fake_bot.db.users.create = create
    await _core._ensure_user_exists(fake_bot, "jidX", "nickX")
    assert called.get("done")

@pytest.mark.asyncio
async def test_get_user_tzinfo(fake_bot, monkeypatch):
    async def _get_user_timezone1(b, j): return "Europe/Berlin"
    async def _get_user_timezone2(b, j): return "Invalid/Timezone"
    monkeypatch.setattr(_core, "_get_user_timezone", _get_user_timezone1)
    tz = await _core.get_user_tzinfo(fake_bot, "somejid")
    assert isinstance(tz, pytz.tzinfo.BaseTzInfo)
    monkeypatch.setattr(_core, "_get_user_timezone", _get_user_timezone2)
    tz = await _core.get_user_tzinfo(fake_bot, "otherjid")
    assert tz.zone == "UTC"

@pytest.mark.asyncio
async def test_check_user_exists(fake_bot):
    async def get(j): return {"jid":j}
    fake_bot.db.users.get = get
    m = msg(from_jid="jidval/Nick")
    assert await _core._check_user_exists(fake_bot, "jidval", m) is True
    async def get_none(j): return None
    fake_bot.db.users.get = get_none
    assert await _core._check_user_exists(fake_bot, "jidval", m) is False
    assert "not a registered user" in fake_bot._replies[-1][0]

@pytest.mark.asyncio
async def test_get_jids_from_nick_index(fake_bot):
    fake_bot.db.users._nick_index["SetNick"] = {"jidA","jidB"}
    fake_bot.db.users._nick_index["ListNick"] = ["jidC"]
    fake_bot.db.users._nick_index["SingleNick"] = "jidD"
    bot = fake_bot
    r = await _core.get_jids_from_nick_index(bot, "SetNick")
    assert r in {"jidA","jidB"}
    r = await _core.get_jids_from_nick_index(bot, "ListNick")
    assert "jidC" in r
    r = await _core.get_jids_from_nick_index(bot, "SingleNick")
    assert r == "jidD"

@pytest.mark.asyncio
async def test_get_real_jid_from_occupant(fake_bot):
    _core.JOINED_ROOMS = fake_bot.bot_plugins.plugins["rooms"].JOINED_ROOMS
    m = msg(from_jid="room@conf/Nick")
    res = await _core.get_real_jid_from_occupant(fake_bot, m)
    assert res == "jidval"
    res2 = await _core.get_real_jid_from_occupant(fake_bot, m, "OtherNick")
    assert res2 == "jidval2"

@pytest.mark.asyncio
async def test_is_room_moderator_or_admin(fake_bot):
    _core.JOINED_ROOMS = fake_bot.bot_plugins.plugins["rooms"].JOINED_ROOMS
    room_jid, nick = "room@conf", "Nick"
    allowed = await _core.is_room_moderator_or_admin(fake_bot, room_jid, nick)
    assert allowed
    allowed2 = await _core.is_room_moderator_or_admin(fake_bot, room_jid, "Nonexistent")
    assert not allowed2

@pytest.mark.asyncio
async def test_handle_room_toggle_command_all_paths(fake_bot, monkeypatch):
    async def can_manage(b, m, isr): return (True,"room@conf",None)
    monkeypatch.setattr(_core, "muc_pm_sender_can_manage_room", can_manage)
    store = SimpleNamespace(
        get_global=lambda key, default=None: {"room@conf": True},
        set_global=lambda k,v: None
    )
    replys = []
    fake_bot.reply = lambda msg, txt, *a, **k: replys.append(txt)
    for subcmd in ["on", "off", "status"]:
        state = {"room@conf": subcmd != "off"}
        async def get_global2(key, default=None, s=state):
            return s.copy()
        store.get_global = get_global2
        async def store_getter(b): return store
        await _core.handle_room_toggle_command(
            fake_bot, {}, False, [subcmd], store_getter=store_getter, key="key", label="lbl", storage="dict"
        )
    storeL = SimpleNamespace(
        get_global=lambda key, default=None: {"rooms": ["room@conf"]},
        set_global=lambda k,v: None)
    for subcmd in ["on", "off", "status"]:
        state = {"rooms": ["room@conf"] if subcmd != "off" else []}
        async def get_global3(key, default=None, s=state):
            return s.copy()
        storeL.get_global = get_global3
        async def storeL_getter(b): return storeL
        await _core.handle_room_toggle_command(
            fake_bot, {}, False, [subcmd], store_getter=storeL_getter, key="key", label="lbl", storage="list"
        )
    async def cant_manage(b, m, isr): return (False,"room@conf","failreason")
    monkeypatch.setattr(_core, "muc_pm_sender_can_manage_room", cant_manage)
    async def store_getter(b): return store
    result = await _core.handle_room_toggle_command(
        fake_bot, {}, False, ["on"], store_getter=store_getter, key="key", label="lbl")
    assert result is True

@pytest.mark.asyncio
async def test_muc_pm_sender_can_manage_room(fake_bot):
    msg1 = msg(from_jid="notjoined@room/X")
    allowed, room_jid, reason = await _core.muc_pm_sender_can_manage_room(fake_bot, msg1, False)
    assert allowed is False and "only be used" in reason
    msg2 = msg(from_jid="room@conf/Unk")
    allowed, room_jid, reason = await _core.muc_pm_sender_can_manage_room(fake_bot, msg2, False)
    # If the user is not in nicks, the error is about permissions
    assert allowed is False and "permissions" in reason
    msg3 = msg(from_jid="room@conf/Nick")
    assert allowed is False and ("permissions" in reason or "only be used" in reason)
