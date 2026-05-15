import pytest
from unittest.mock import MagicMock, AsyncMock
import types
from urllib.parse import urljoin

import plugins.urlcheck as urlcheck

# ---- msg class for all event handler tests ----
class MsgNS(types.SimpleNamespace):
    def __getitem__(self, key):
        return getattr(self, key)
    def get(self, key, default=None):
        return getattr(self, key, default)

@pytest.fixture
def fake_bot(monkeypatch):
    bot = MagicMock()
    class FakeDB:
        users = MagicMock()
    bot.db = FakeDB()
    # Simulate a real async DummyStore with async get_global/set_global
    class DummyStore:
        def __init__(self):
            self.data = {}
        async def get_global(self, key, default=None):
            return dict(self.data)
        async def set_global(self, key, value):
            self.data.update(value)
    dummy_store = DummyStore()
    def plugin_side_effect(key):
        assert key == "urlcheck"
        return dummy_store
    bot.db.users.plugin = plugin_side_effect  # <-- returns DummyStore, not AsyncMock/coroutine!
    bot._test_urlcheck_store = dummy_store
    bot._replies = []
    bot.reply = lambda msg, body, **kwargs: bot._replies.append((msg, body, kwargs))
    class DummyMsg:
        def __init__(self): self.sent = False
        def send(self): self.sent = True
        def __setitem__(self, k, v): setattr(self, k, v)
        def __getitem__(self, k): return getattr(self, k)
        xml = types.SimpleNamespace(findall=lambda self, *a, **k: [])
    bot.make_message = lambda **kwargs: DummyMsg()
    return bot

@pytest.mark.asyncio
async def test_urlcheck_toggle_commands(fake_bot):
    store = fake_bot._test_urlcheck_store
    store.data.clear()
    for cmd in [["on"], ["off"], ["status"]]:
        msg = {"from": type("F", (), {"bare": "room@conf"})(), "body": ",urlcheck " + (cmd[0] if cmd else "")}
        await urlcheck.urlcheck_command(fake_bot, "sender", "nick", cmd, msg, True)
    assert isinstance(await store.get_global("any"), dict)
    msg = {"from": type("F", (), {"bare": "room@conf"})(), "body": ",urlcheck "}
    await urlcheck.urlcheck_command(fake_bot, "sender", "nick", [], msg, True)

def test_resolve_url_with_urljoin():
    # Only test urljoin, since there is no urlcheck._normalize_url
    base = "https://foo.com/x/"
    assert urljoin(base, "/b") == "https://foo.com/b"
    assert urljoin("https://foo.com/", "http://other.net") == "http://other.net"
    assert urljoin("https://site/root/", "dir/page.html") == "https://site/root/dir/page.html"

@pytest.mark.asyncio
async def test_fetch_url_title_basic(monkeypatch):
    from requests import Session
    called = []
    class FakeResp:
        url = "https://final"
        headers = {"Content-Type": "text/html"}
        status_code = 200
        def iter_content(self, chunk_size, decode_unicode):
            yield "<title>X</title><meta name='description' content='desc'>"
        def close(self): pass
    class MySession(Session):
        def __init__(self): self._headers = {}
        @property
        def headers(self): return self._headers
        def get(self, url, allow_redirects, timeout, stream):
            called.append(url)
            return FakeResp()
        def close(self): pass
    monkeypatch.setattr(urlcheck, "requests", MagicMock(Session=MySession))
    final_url, status, ctype, title, _, desc = urlcheck.fetch_url_title("https://xx")
    assert final_url == "https://final"
    assert status == 200
    assert "text/html" in ctype
    assert title == "X"
    assert desc == "desc"

def test_youtube_regex():
    m = urlcheck.YOUTUBE_RE.search("https://youtu.be/abcdefghijk")
    assert m
    assert m.group(1) == "abcdefghijk"
    m = urlcheck.YOUTUBE_RE.search("https://youtube.com/watch?v=abcdefghijk")
    assert m
    assert m.group(2) == "abcdefghijk"

@pytest.mark.asyncio
async def test_fetch_youtube_info(monkeypatch):
    urlcheck.config["youtube_api_key"] = "fake-api-key"
    class DummyResp:
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc, tb): pass
        async def json(self): return {"items": [{
            "snippet": {"title": "t", "channelTitle": "ch", "publishedAt": "2022-01-01T00:00:00Z"},
            "statistics": {"viewCount": "1"},
            "contentDetails": {"duration": "PT12M34S"}
        }]}
        @property
        def status(self): return 200
    class DummySession:
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc, tb): pass
        def get(self, url, timeout):
            return DummyResp()
    monkeypatch.setattr(urlcheck.aiohttp, "ClientSession", MagicMock(return_value=DummySession()))
    val = await urlcheck.fetch_youtube_info("https://youtu.be/12345678901")
    assert isinstance(val, tuple)
    assert "ch" in val[0]

# ---------- EVENT HANDLER (on_groupchat_message) TESTS ----------

def msg_ns_dict(**kwargs):
    class Msg(MsgNS):  # uses MsgNS with .get, __getitem__
        pass
    return Msg(**kwargs)

@pytest.mark.asyncio
async def test_on_groupchat_message_disabled_does_nothing(fake_bot, monkeypatch):
    store = fake_bot._test_urlcheck_store
    store.data.clear()
    room_jid = "room1@conf"
    urlcheck.JOINED_ROOMS[room_jid] = {"nick": "me"}
    msg = msg_ns_dict(
        **{"from": msg_ns_dict(bare=room_jid, resource="user1"),
           "mucnick": "user1",
           "body": "http://test.site",
           "type": "groupchat",
           "xml": types.SimpleNamespace(find=lambda p: None)}
    )
    monkeypatch.setattr(urlcheck, "fetch_url_title", lambda *a, **k: pytest.fail("fetch_url_title called when feature off"))
    await urlcheck.on_groupchat_message(fake_bot, msg)
    assert fake_bot._replies == []

@pytest.mark.asyncio
async def test_on_groupchat_message_self_suppression(fake_bot, monkeypatch):
    store = fake_bot._test_urlcheck_store
    store.data["room2@conf"] = True
    urlcheck.JOINED_ROOMS["room2@conf"] = {"nick": "botnick"}
    msg = msg_ns_dict(
        **{"from": msg_ns_dict(bare="room2@conf", resource="botnick"),
           "mucnick": "botnick",
           "body": "https://some.url",
           "type": "groupchat",
           "xml": types.SimpleNamespace(find=lambda p: None)}
    )
    monkeypatch.setattr(urlcheck, "fetch_url_title", lambda *a, **k: pytest.fail("bot's own messages should not be handled"))
    await urlcheck.on_groupchat_message(fake_bot, msg)
    assert fake_bot._replies == []

@pytest.mark.asyncio
async def test_on_groupchat_message_regular_url(fake_bot, monkeypatch):
    store = fake_bot._test_urlcheck_store
    store.data["room3@conf"] = True
    urlcheck.JOINED_ROOMS["room3@conf"] = {"nick": "someone"}
    monkeypatch.setattr(urlcheck, "fetch_url_title",
        lambda url, max_redirects=5: ("http://real", 200, "text/html", "HTML Title", 123, "mydesc"))
    monkeypatch.setattr(urlcheck, "is_youtube_url", lambda url: False)
    monkeypatch.setattr(urlcheck, "has_xep_0392_link_metadata", lambda msg: False)
    called_send = []
    orig_make_message = fake_bot.make_message
    def fake_make_message(**kwargs):
        m = orig_make_message(**kwargs)
        def send():
            called_send.append(True)
        m.send = send
        return m
    fake_bot.make_message = fake_make_message
    msg = msg_ns_dict(
        **{"from": msg_ns_dict(bare="room3@conf", resource="alice"),
           "mucnick": "alice",
           "body": "https://with.ti.tle",
           "type": "groupchat",
           "xml": types.SimpleNamespace(find=lambda p: None)}
    )
    await urlcheck.on_groupchat_message(fake_bot, msg)
    assert called_send

@pytest.mark.asyncio
async def test_on_groupchat_message_youtube_url(fake_bot, monkeypatch):
    store = fake_bot._test_urlcheck_store
    store.data["room4@conf"] = True
    urlcheck.JOINED_ROOMS["room4@conf"] = {"nick": "someone"}
    monkeypatch.setattr(urlcheck, "fetch_url_title",
        lambda url, max_redirects=5: ("http://yt.vid", 200, "text/html", "Title", 321, "desc"))
    monkeypatch.setattr(urlcheck, "is_youtube_url", lambda url: True)
    monkeypatch.setattr(urlcheck, "fetch_youtube_info", AsyncMock(return_value=("YOUTUBE DESC", "thetitle", "uploader", "4m3s", 123)))
    monkeypatch.setattr(urlcheck, "has_xep_0392_link_metadata", lambda msg: False)
    called_send = []
    orig_make_message = fake_bot.make_message
    def fake_make_message(**kwargs):
        m = orig_make_message(**kwargs)
        def send():
            called_send.append(True)
        m.send = send
        return m
    fake_bot.make_message = fake_make_message
    msg = msg_ns_dict(
        **{"from": msg_ns_dict(bare="room4@conf", resource="bob"),
           "mucnick": "bob",
           "body": "https://youtube.com/watch?v=ABCDEFGHIJK",
           "type": "groupchat",
           "xml": types.SimpleNamespace(find=lambda p: None)}
    )
    await urlcheck.on_groupchat_message(fake_bot, msg)
    assert called_send

@pytest.mark.asyncio
async def test_on_groupchat_message_codeblock_and_quote_suppression(fake_bot, monkeypatch):
    store = fake_bot._test_urlcheck_store
    store.data["room5@conf"] = True
    urlcheck.JOINED_ROOMS["room5@conf"] = {"nick": "notme"}
    monkeypatch.setattr(urlcheck, "fetch_url_title", lambda *a, **k: pytest.fail("Should not fetch inside codeblock/quote"))
    msg = msg_ns_dict(
        **{"from": msg_ns_dict(bare="room5@conf", resource="dave"),
           "mucnick": "dave",
           "body": "> quoted url\n```http://codeblock.url```\n    http://indented.url",
           "type": "groupchat",
           "xml": types.SimpleNamespace(find=lambda p: None)}
    )
    await urlcheck.on_groupchat_message(fake_bot, msg)
    assert fake_bot._replies == []
