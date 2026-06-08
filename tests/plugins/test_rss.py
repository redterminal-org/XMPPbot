import pytest
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import plugins.rss as rss
import plugins.rooms


# Patch rss.config for all tests to support both subscript and get
# (for legacy plugin code)
@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    class DummyConfig(dict):
        def __getitem__(self, key):
            if isinstance(key, tuple):
                k, default = key
            else:
                k, default = key, None

            if k == "prefix":
                return ","

            return default or ","

        def get(self, key, default=None):
            if key == "prefix":
                return ","

            return default or ","

    monkeypatch.setattr(rss, "config", DummyConfig())


@pytest.fixture
def make_bot():
    """
    Return a fake bot object with pluggable bot.reply and db.users.plugin().
    """

    class DummyStore(dict):
        async def get_global(self, key, default=None):
            return self.get(key, default if default is not None else {})

        async def set_global(self, key, value):
            self[key] = value

    class DummyBot:
        def __init__(self):
            self.replies = []
            self.flush_count = 0

            async def flush_all():
                self.flush_count += 1

            self.db = SimpleNamespace(
                users=SimpleNamespace(
                    plugin=lambda name: self.plugin_store,
                    flush_all=flush_all,
                )
            )
            self.plugin_store = DummyStore()

        def reply(self, msg, text, **kwargs):
            self.replies.append((msg, text, kwargs))

    return DummyBot


@pytest.mark.asyncio
async def test_rss_add_list_delete(monkeypatch, make_bot):
    bot = make_bot()
    store = bot.plugin_store
    room = "room@conference.example.org"
    fake_feed_title = "TestFeed"
    fake_feed_link = "https://www.example.com/rss"
    fake_feed_entry = {
        "title": "EntryTitle",
        "link": "https://www.example.com/article",
        "description": "EntryDesc",
        "id": "https://www.example.com/article",
    }

    # Patch feedparser.parse (for plugin coverage)
    monkeypatch.setattr(rss, "feedparser", type("Feedparser", (), {})())

    class DummyFeed:
        def __init__(self):
            self.feed = {
                "title": fake_feed_title,
                "link": fake_feed_link,
                "href": fake_feed_link,
                "id": fake_feed_link,
            }
            self.entries = [SimpleNamespace(**fake_feed_entry)]

        def __contains__(self, k):
            # needed for some plugin code
            return k == "feed"

    async def fake_fetch_feed(url):
        return DummyFeed()

    monkeypatch.setattr(rss, "fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(rss, "ensure_task", AsyncMock())

    msg = {"from": SimpleNamespace(bare=room), "type": "groupchat"}

    # Add
    await rss.rss_command(bot, "jid1", "nick1", ["add", fake_feed_link],
                          msg, True)
    feeds = store.get(rss.RSS_KEY, {})
    assert fake_feed_link in feeds
    assert bot.flush_count >= 1

    # Add again to test 'already in feed' and room-join path
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["add", fake_feed_link],
                          msg, True)
    assert any("already added" in x[1]
               or "Added room" in x[1] for x in bot.replies)

    # List
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["list"], msg, True)
    assert any("Watched RSS feeds" in x[1][0] for x in bot.replies)

    # Delete (should remove the only room, triggers feed delete in dummy)
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["delete", fake_feed_link],
                          msg, True)
    assert any(
        "no rooms left" in x[1]
        or "Removed this room" in x[1] for x in bot.replies)

    # Delete again (feed not found)
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["delete", fake_feed_link],
                          msg, True)
    assert any("Feed not found" in x[1] for x in bot.replies)

    # Add missing arg
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["add"], msg, True)
    assert any("Usage:" in x[1] for x in bot.replies)

    # Delete missing arg
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["delete"], msg, True)
    assert any("Usage:" in x[1] for x in bot.replies)

    # List with no feeds (store reset)
    bot.plugin_store.clear()
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["list"], msg, True)
    assert any("No feeds configured" in x[1] for x in bot.replies)

    # Unknown subcommand
    bot.replies.clear()
    await rss.rss_command(bot, "jid1", "nick1", ["foobar"], msg, True)
    assert any("Unknown subcommand" in x[1] for x in bot.replies)


@pytest.mark.asyncio
async def test_fetch_feed_handle_redirect_and_structure(monkeypatch):
    class DummyFeed:
        def __init__(self, url):
            self.feed = {"title": "Test", "link": url}
            self.entries = []

        def __contains__(self, k):
            return k == "feed"

    def fake_parse(url, request_headers=None):
        return DummyFeed(url)

    feedparser_mod = type(
        "Feedparser", (), {"parse": staticmethod(fake_parse)})()
    monkeypatch.setattr(rss, "feedparser", feedparser_mod)

    async def fake_to_thread(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = await rss.fetch_feed("https://someurl.com/feed")

    assert result.feed["href"] == "https://someurl.com/feed"
    assert result.feed["id"] == "https://someurl.com/feed"
    assert result.feed["title"] == "Test"


@pytest.mark.asyncio
async def test_should_include_description():
    title = "Title"
    assert not rss._should_include_description(title, "")
    assert not rss._should_include_description(title, "Title")
    assert not rss._should_include_description("hi", "hi more stuff")
    assert not rss._should_include_description("foo bar baz", "foo bar")
    assert not rss._should_include_description("aaaaaa", "aaaaab")
    assert rss._should_include_description("aaa", "bbbccc")


def test_generate_entry_id():
    t, d, lnk = "Title", "Desc", "http://a/"

    assert rss._generate_entry_id(t, d, lnk) == lnk

    id1 = rss._generate_entry_id("t1", "d1", "")
    id2 = rss._generate_entry_id("t1", "d1", None)
    id3 = rss._generate_entry_id("t1", "d1", "")

    assert id1 == id3 and id2 == id1


def test_get_entry_id():
    entry = {
        "title": "Title",
        "description": "Description",
        "link": "https://example.org/post",
    }

    assert rss._get_entry_id(entry) == "https://example.org/post"


def test_get_latest_entry_id():
    class DummyParsed:
        entries = [
            {
                "title": "Newest",
                "description": "Newest description",
                "link": "https://example.org/newest",
            },
            {
                "title": "Older",
                "description": "Older description",
                "link": "https://example.org/older",
            },
        ]

    assert rss._get_latest_entry_id(
        DummyParsed()) == "https://example.org/newest"

    class EmptyParsed:
        entries = []

    assert rss._get_latest_entry_id(EmptyParsed()) is None


def test_normalize_and_resolve_url():
    assert rss._normalize_url("EXAMPLE.COM/abc/") == "https://EXAMPLE.COM/abc"
    assert rss._normalize_url("http://abc.com") == "http://abc.com"

    assert (
        rss._resolve_relative_url(
            "https://foo.com/feed", "https://bar.com/page")
        == "https://bar.com/page"
    )
    assert rss._resolve_relative_url(
        "https://foo.com/feed", "/bar") == "https://foo.com/bar"
    assert rss._resolve_relative_url(None, "/foo") == "/foo"


def test_extract_entry_link_variants():
    # Supports both dict and attr-based entry (for plugin coverage)
    class AtomEntryObj(dict):
        def __init__(self):
            self.links = [{"rel": "alternate", "href": "http://example.com"}]

        def __contains__(self, key):
            return key == "links"

        def get(self, key, default=None):
            if key == "links":
                return self.links
            return default

    e = {"link": "http://a.com"}
    assert rss._extract_entry_link(e) == "http://a.com"

    atom_e = AtomEntryObj()
    assert rss._extract_entry_link(atom_e) == "http://example.com"

    e3 = {"url": "https://feed/item"}
    assert rss._extract_entry_link(e3) == "https://feed/item"

    e4 = {"id": "https://idvalue/"}
    assert rss._extract_entry_link(e4) == "https://idvalue/"

    assert rss._extract_entry_link({}) == ""


@pytest.mark.asyncio
async def test_rss_add_failures(monkeypatch, make_bot):
    bot = make_bot()

    monkeypatch.setattr(rss, "feedparser", type("Feedparser", (), {})())

    async def raise_exc(url):
        raise Exception("bad feed")

    monkeypatch.setattr(rss, "fetch_feed", raise_exc)
    monkeypatch.setattr(rss, "ensure_task", AsyncMock())

    msg = {"from": SimpleNamespace(bare="room@conf"), "type": "groupchat"}

    await rss.rss_command(bot, "jid", "nick", ["add", "http://bad/feed"],
                          msg, True)

    assert any("Failed to fetch or parse feed" in r[1] for r in bot.replies)


@pytest.mark.asyncio
async def test_rss_check_loop_initializes_missing_last_id_without_posting(
        monkeypatch, make_bot):
    bot = make_bot()
    store = bot.plugin_store
    url = "http://f.com/rss"
    room = "room@conference.example.org"

    store[rss.RSS_KEY] = {
        url: {
            "title": "Feed",
            "link": url,
            "period": 1,
            "rooms": [room],
            "last_id": None,
            "error_count": 0,
            "next_retry": 0,
        }
    }

    # Key step for your plugin: JOINED_ROOMS is a dict, not set
    plugins.rooms.JOINED_ROOMS[room] = True

    class Entry(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            for k, v in kwargs.items():
                setattr(self, k, v)

        def get(self, k, default=None):
            if hasattr(self, k):
                return getattr(self, k)
            if k in self:
                return self[k]
            return default

    entry = Entry(
        title="ET",
        link="http://f.com/a1",
        description="ED",
        id="http://f.com/a1",
    )

    class DummyFeed:
        def __init__(self):
            self.feed = {"title": "Feed", "link": url, "href": url, "id": url}
            self.entries = [entry]

        def __contains__(self, k):
            return k == "feed"

    async def fetch_feed(_):
        return DummyFeed()

    monkeypatch.setattr(rss, "fetch_feed", fetch_feed)
    monkeypatch.setattr(rss, "_now", lambda: 1000)

    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    posts = []

    def fake_reply(msg, txt, **kwargs):
        posts.append(("reply", txt, kwargs))

    bot.reply = fake_reply

    try:
        with pytest.raises(asyncio.CancelledError):
            await rss.rss_check_loop(bot, store, url, 1)

        assert posts == []
        assert store[rss.RSS_KEY][url]["last_id"] == "http://f.com/a1"
        assert bot.flush_count >= 1
    finally:
        # Clean up global to avoid leaking state between tests
        plugins.rooms.JOINED_ROOMS.pop(room, None)


@pytest.mark.asyncio
async def test_rss_check_loop_posts_new_entries_and_flushes_last_id(
        monkeypatch, make_bot):
    bot = make_bot()
    store = bot.plugin_store
    url = "http://f.com/rss"
    room = "room@conference.example.org"

    store[rss.RSS_KEY] = {
        url: {
            "title": "Feed",
            "link": url,
            "period": 1,
            "rooms": [room],
            "last_id": "http://f.com/a1",
            "error_count": 0,
            "next_retry": 0,
        }
    }

    plugins.rooms.JOINED_ROOMS[room] = True

    class Entry(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            for k, v in kwargs.items():
                setattr(self, k, v)

        def get(self, k, default=None):
            if hasattr(self, k):
                return getattr(self, k)
            if k in self:
                return self[k]
            return default

    newest_entry = Entry(
        title="ET2",
        link="http://f.com/a2",
        description="ED2",
        id="http://f.com/a2",
    )
    old_entry = Entry(
        title="ET1",
        link="http://f.com/a1",
        description="ED1",
        id="http://f.com/a1",
    )

    class DummyFeed:
        def __init__(self):
            self.feed = {"title": "Feed", "link": url, "href": url, "id": url}
            self.entries = [newest_entry, old_entry]

        def __contains__(self, k):
            return k == "feed"

    async def fetch_feed(_):
        return DummyFeed()

    monkeypatch.setattr(rss, "fetch_feed", fetch_feed)
    monkeypatch.setattr(rss, "_now", lambda: 1000)

    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    posts = []

    def fake_reply(msg, txt, **kwargs):
        posts.append(("reply", txt, kwargs))

    bot.reply = fake_reply

    try:
        with pytest.raises(asyncio.CancelledError):
            await rss.rss_check_loop(bot, store, url, 1)

        assert len(posts) == 1
        assert "ET2" in posts[0][1]
        assert "http://f.com/a2" in posts[0][1]
        assert store[rss.RSS_KEY][url]["last_id"] == "http://f.com/a2"
        assert bot.flush_count >= 1
    finally:
        plugins.rooms.JOINED_ROOMS.pop(room, None)


@pytest.mark.asyncio
async def test_rss_check_loop_backoff_flushes_state(monkeypatch, make_bot):
    bot = make_bot()
    store = bot.plugin_store
    url = "http://f.com/rss"
    room = "room@conference.example.org"

    store[rss.RSS_KEY] = {
        url: {
            "title": "Feed",
            "link": url,
            "period": 1,
            "rooms": [room],
            "last_id": "http://f.com/a1",
            "error_count": 0,
            "next_retry": 0,
        }
    }

    async def fetch_feed(_):
        raise Exception("fetch failed")

    monkeypatch.setattr(rss, "fetch_feed", fetch_feed)
    monkeypatch.setattr(rss, "_now", lambda: 1000)

    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await rss.rss_check_loop(bot, store, url, 1)

    assert store[rss.RSS_KEY][url]["error_count"] == 1
    assert store[rss.RSS_KEY][url]["next_retry"] > 1000
    assert bot.flush_count >= 1


@pytest.mark.asyncio
async def test_on_load_unload_calls(monkeypatch, make_bot):
    bot = make_bot()

    monkeypatch.setattr(rss, "feedparser", type("Feedparser", (), {})())

    restart = AsyncMock()
    monkeypatch.setattr(rss, "restart_all_tasks", restart)

    await rss.on_load(bot)
    assert restart.awaited

    t = asyncio.create_task(asyncio.sleep(0.01))
    rss.CHECK_TASKS["foo"] = t

    await rss.on_unload(bot)

    assert "foo" not in rss.CHECK_TASKS or rss.CHECK_TASKS["foo"].done()
