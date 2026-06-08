import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import plugins.xkcd as xkcd


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.make_message = MagicMock(return_value=MagicMock())
    bot.reply = MagicMock()
    return bot

#
# --- normalize_image_url
#


def test_normalize_image_url_variants():
    assert xkcd.normalize_image_url(None) is None
    assert xkcd.normalize_image_url("") is None
    assert (xkcd.normalize_image_url(
        "https://imgs.xkcd.com/comics/test.png")
            == "https://imgs.xkcd.com/comics/test.png")
    assert (xkcd.normalize_image_url(
        "//imgs.xkcd.com/comics/test.png")
            == "https://imgs.xkcd.com/comics/test.png")
    assert xkcd.normalize_image_url(
        "/comics/test.png") == "https://imgs.xkcd.com/comics/test.png"
    assert xkcd.normalize_image_url("other.png") == "other.png"

#
# --- format_comic_message
#


def test_format_comic_message():
    comic = {"num": 42, "title": "The Answer", "alt": "Alt text"}
    msg = xkcd.format_comic_message(comic)
    assert "42" in msg
    assert "The Answer" in msg
    assert "Alt text" in msg
    assert "https://xkcd.com/42" in msg


def test_format_comic_message_minimal():
    comic = {}
    msg = xkcd.format_comic_message(comic)
    assert "#?" in msg
    assert "No title" in msg

#
# --- fetch_xkcd/get_latest_xkcd/get_xkcd
#


@pytest.mark.asyncio
async def test_fetch_xkcd_success(monkeypatch):
    class DummyResp:
        status = 200
        async def json(self): return {"num": 1}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class DummySession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, timeout=None): return DummyResp()

    monkeypatch.setattr("aiohttp.ClientSession", lambda: DummySession())
    url = "https://xkcd.com/1/info.0.json"
    data = await xkcd.fetch_xkcd(url)
    assert data["num"] == 1


@pytest.mark.asyncio
async def test_fetch_xkcd_http_error(monkeypatch):
    class DummyResp:
        status = 404
        async def json(self): return {"error": 404}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class DummySession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, timeout=None): return DummyResp()

    monkeypatch.setattr("aiohttp.ClientSession", lambda: DummySession())
    data = await xkcd.fetch_xkcd("https://xkcd.com/404/info.0.json")
    assert data is None


@pytest.mark.asyncio
async def test_fetch_xkcd_exception(monkeypatch):
    class DummySession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, timeout=None): raise Exception("fail")
    monkeypatch.setattr("aiohttp.ClientSession", lambda: DummySession())
    data = await xkcd.fetch_xkcd("https://xkcd.com/1/info.0.json")
    assert data is None


@pytest.mark.asyncio
async def test_get_latest_xkcd(monkeypatch):
    called = {}

    async def fakefetch(url, session=None):
        called['url'] = url
        return {"num": 2222}
    monkeypatch.setattr(xkcd, "fetch_xkcd", fakefetch)
    result = await xkcd.get_latest_xkcd()
    assert "url" in called and xkcd.XKCD_LATEST_URL in called["url"]
    assert result["num"] == 2222


@pytest.mark.asyncio
async def test_get_xkcd(monkeypatch):
    test_id = 5

    async def fakefetch(url, session=None):
        assert str(test_id) in url
        return {"num": test_id}
    monkeypatch.setattr(xkcd, "fetch_xkcd", fakefetch)
    result = await xkcd.get_xkcd(test_id)
    assert result["num"] == 5

#
# --- send_url_with_oob
#


@pytest.mark.asyncio
async def test_send_url_with_oob_sets_field_and_sends():
    bot = MagicMock()
    msg_obj = MagicMock()
    bot.make_message.return_value = msg_obj

    class OOB:
        def __setitem__(self, k, v): self.url = v

    def getitem(key):
        if key == "oob":
            return OOB()
        raise KeyError(key)
    msg_obj.__getitem__.side_effect = getitem
    msg_obj.send = MagicMock()

    await xkcd.send_url_with_oob(bot, "jid@xmpp", "http://test", "chat")
    msg_obj.send.assert_called()


@pytest.mark.asyncio
async def test_send_url_with_oob_attach_oob_fails():
    bot = MagicMock()
    msg_obj = MagicMock()
    bot.make_message.return_value = msg_obj
    msg_obj.__getitem__.side_effect = Exception("No OOB")
    msg_obj.send = MagicMock()
    await xkcd.send_url_with_oob(bot, "jid@xmpp", "http://test", "chat")
    msg_obj.send.assert_called()

#
# --- send_xkcd_room / send_xkcd_dm
#


@pytest.mark.asyncio
async def test_send_xkcd_room_success(mock_bot):
    comic = {"img": "/comics/foo.png", "num": 13, "title": "foo"}
    with (patch("plugins.xkcd.send_url_with_oob", new_callable=AsyncMock)
          as send_oob):
        await xkcd.send_xkcd_room(mock_bot, "roomid@chat", comic)
        mock_bot.reply.assert_called()
        send_oob.assert_awaited()


@pytest.mark.asyncio
async def test_send_xkcd_room_no_img(mock_bot):
    comic = {"num": 5, "title": "fail"}
    with patch("plugins.xkcd.send_url_with_oob", new_callable=AsyncMock):
        await xkcd.send_xkcd_room(mock_bot, "room@conf", comic)
        mock_bot.reply.assert_not_called()


@pytest.mark.asyncio
async def test_send_xkcd_dm_success(mock_bot):
    comic = {"img": "/comics/bar.png", "num": 85, "title": "bar"}
    msg_obj = MagicMock()
    mock_bot.make_message.return_value = msg_obj
    msg_obj.send = MagicMock()
    with (patch("plugins.xkcd.send_url_with_oob", new_callable=AsyncMock)
          as send_oob):
        await xkcd.send_xkcd_dm(mock_bot, "me@xmpp", comic)
        msg_obj.send.assert_called()
        send_oob.assert_awaited()


@pytest.mark.asyncio
async def test_send_xkcd_dm_no_img(mock_bot):
    comic = {"title": "badcomic"}
    msg_obj = MagicMock()
    mock_bot.make_message.return_value = msg_obj
    msg_obj.send = MagicMock()
    with patch("plugins.xkcd.send_url_with_oob", new_callable=AsyncMock):
        await xkcd.send_xkcd_dm(mock_bot, "me@xmpp", comic)
        msg_obj.send.assert_not_called()
