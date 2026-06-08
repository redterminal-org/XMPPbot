import pytest
import types
import asyncio
import csv
from plugins import info as info_plugin

# ---- AIOHTTP ASYNC CTX MOCKING HELPERS ----


class AsyncContextResp:
    """Simulates async context manager for aiohttp response."""

    def __init__(self, status, json_data):
        self.status = status
        self._json_data = json_data

    async def json(self):
        return self._json_data

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): pass


class DummyAioSession:
    """Simulate aiohttp.ClientSession with url -> AsyncContextResp map."""

    def __init__(self, resp_map=None):
        self.resp_map = resp_map or {}

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): pass

    def get(self, url, timeout=None):
        for key in self.resp_map:
            if key in url:
                return self.resp_map[key]
        if self.resp_map:
            return list(self.resp_map.values())[0]
        else:
            return AsyncContextResp(500, {})


# ---- BOT/MSG FIXTURES ----

class DummyBot:
    def __init__(self):
        self.replies = []
        self.prefix = ","
        self.version = "test"
        self.db = types.SimpleNamespace()
        self.db.users = types.SimpleNamespace()
        self.bot_plugins = types.SimpleNamespace()
        self.bot_plugins.plugins = {"info": info_plugin}
        self.bot_plugins.register_event = lambda *a, **k: None
        self.bot_plugins.list = lambda: ["info"]

    def reply(self, msg, text, **kwargs):
        self.replies.append((msg, text))

    def reset(self):
        self.replies.clear()


@pytest.fixture
def dummy_bot(monkeypatch):
    bot = DummyBot()

    class DummyPlugin:
        def __init__(self):
            self._data = {}

        async def get_global(
            self, key, default=None): return self._data.get(key, default)

        async def set_global(self, key, value): self._data[key] = value
    dummy_plugin = DummyPlugin()
    bot.db.users.plugin = lambda plugin_name: dummy_plugin
    monkeypatch.setattr(info_plugin, "get_info_store",
                        lambda bot: dummy_plugin)
    return bot


@pytest.fixture
def fake_room_msg():
    return {
        "from": types.SimpleNamespace(bare="testroom@conf", resource="nick"),
        "body": "",
        "type": "groupchat"
    }


@pytest.fixture
def fake_dm_msg():
    return {
        "from": types.SimpleNamespace(bare="user@domain", resource=None),
        "body": "",
        "type": "chat"
    }


@pytest.fixture(autouse=True)
def patch_enabled_rooms(monkeypatch):
    async def enabled_rooms(bot, key, plugin):
        return {"testroom@conf": True}
    monkeypatch.setattr(info_plugin, "_get_enabled_rooms", enabled_rooms)

# ---- URBAN DICTIONARY ----


@pytest.mark.asyncio
async def test_udict_usage(dummy_bot, fake_room_msg):
    await info_plugin.udict_search(dummy_bot, "jid", "nick", [],
                                   fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "Usage" in text


@pytest.mark.asyncio
async def test_udict_flow_found(monkeypatch, dummy_bot, fake_room_msg):
    resp = AsyncContextResp(200, {"list": [{
        "definition": "some meaning", "example": "an example",
        "thumbs_up": 5, "thumbs_down": 1, "permalink": "url"
    }]})
    monkeypatch.setattr(info_plugin.aiohttp, "ClientSession",
                        lambda: DummyAioSession({"udict": resp}))
    dummy_bot.reset()
    await info_plugin.udict_search(dummy_bot, "jid", "nick", ["test"],
                                   fake_room_msg, True)
    text = "\n".join(map(str, dummy_bot.replies))
    assert "Definition:" in text and "Example:" in text and "👍" in text


@pytest.mark.asyncio
async def test_udict_flow_not_found(monkeypatch, dummy_bot, fake_room_msg):
    resp = AsyncContextResp(200, {"list": []})
    monkeypatch.setattr(info_plugin.aiohttp, "ClientSession",
                        lambda: DummyAioSession({"udict": resp}))
    dummy_bot.reset()
    await info_plugin.udict_search(dummy_bot, "jid", "nick", ["test"],
                                   fake_room_msg, True)
    text = "\n".join(map(str, dummy_bot.replies))
    assert "No definitions" in text


@pytest.mark.asyncio
async def test_udict_error(monkeypatch, dummy_bot, fake_room_msg):
    class BrokenSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, *a, **k): raise Exception("fail")
    monkeypatch.setattr(info_plugin.aiohttp,
                        "ClientSession", lambda: BrokenSession())
    dummy_bot.reset()
    await info_plugin.udict_search(dummy_bot, "jid", "nick", ["fail"],
                                   fake_room_msg, True)
    text = "\n".join(map(str, dummy_bot.replies))
    assert "Error fetching" in text


# ---- FEDIVERSE ----

@pytest.mark.asyncio
async def test_fediverse_usage(dummy_bot, fake_room_msg):
    await info_plugin.fediverse_latest(dummy_bot, "jid", "nick", [],
                                       fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "Usage:" in text


@pytest.mark.asyncio
async def test_fediverse_invalid_format(dummy_bot, fake_room_msg):
    await info_plugin.fediverse_latest(dummy_bot, "jid", "nick", ["invalid"],
                                       fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "Please specify the user as" in text


@pytest.mark.asyncio
async def test_fediverse_error(monkeypatch, dummy_bot, fake_room_msg):
    class BrokenSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, *a, **k): raise Exception("fail")
    monkeypatch.setattr(info_plugin.aiohttp,
                        "ClientSession", lambda: BrokenSession())
    dummy_bot.reset()
    await info_plugin.fediverse_latest(dummy_bot, "jid", "nick", ["@foo@bar"],
                                       fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "Error fetching from Fediverse" in text


@pytest.mark.asyncio
async def test_fediverse_nomatches(monkeypatch, dummy_bot, fake_room_msg):
    resp_user = AsyncContextResp(200, {"id": "42"})
    resp_timeline = AsyncContextResp(200, [])
    monkeypatch.setattr(
        info_plugin.aiohttp, "ClientSession",
        lambda: DummyAioSession(
            {"lookup": resp_user, "/statuses": resp_timeline}),
    )
    dummy_bot.reset()
    await info_plugin.fediverse_latest(dummy_bot, "jid", "nick",
                                       ["@someone@host"], fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies).lower()
    assert "no public toots" in text


@pytest.mark.asyncio
async def test_fediverse_success(monkeypatch, dummy_bot, fake_room_msg):
    timeline_content = [{
        "content": "<p>hello <a href=\"url\">link</a></p>",
        "url": "u", "reblogs_count": 1, "replies_count": 2,
        "favourites_count": 3
    }]
    resp_user = AsyncContextResp(200, {"id": "42"})
    resp_timeline = AsyncContextResp(200, timeline_content)
    monkeypatch.setattr(
        info_plugin.aiohttp, "ClientSession",
        lambda: DummyAioSession(
            {"lookup": resp_user, "/statuses": resp_timeline}),
    )
    dummy_bot.reset()
    await info_plugin.fediverse_latest(dummy_bot, "jid", "nick",
                                       ["@someone@host"], fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies).lower()
    assert "toot" in text and "hello" in text

# ---- ACRONYMS (all variants) ----


@pytest.fixture
def tmp_slang_files(tmp_path, monkeypatch):
    main = tmp_path/"chat_slang.csv"
    add = tmp_path/"slang_additions.csv"
    rem = tmp_path/"slang_removals.csv"
    with main.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["lgtm", "Looks good to me"])
    monkeypatch.setattr(info_plugin, "SLANG_CSV", str(main))
    monkeypatch.setattr(info_plugin, "SLANG_ADDITIONS_CSV", str(add))
    monkeypatch.setattr(info_plugin, "SLANG_REMOVALS_CSV", str(rem))
    return main, add, rem


@pytest.mark.asyncio
async def test_acronyms_cmd_found(tmp_slang_files, dummy_bot, fake_room_msg):
    main, _, _ = tmp_slang_files
    dummy_bot.reset()
    await info_plugin.acronyms_cmd(dummy_bot, "jid", "nick", ["lgtm"],
                                   fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "LGTM:" in text


@pytest.mark.asyncio
async def test_acronyms_cmd_not_found(tmp_slang_files, dummy_bot,
                                      fake_room_msg):
    main, _, _ = tmp_slang_files
    with main.open("w", encoding="utf-8"):
        pass
    dummy_bot.reset()
    await info_plugin.acronyms_cmd(dummy_bot, "jid", "nick", ["NOSUCH"],
                                   fake_room_msg, True)
    out = "\n".join(str(x) for x in dummy_bot.replies)
    assert "not defined" in out


@pytest.mark.asyncio
async def test_acronyms_add_cmd(tmp_slang_files, dummy_bot, fake_room_msg):
    _, add, _ = tmp_slang_files
    dummy_bot.reset()
    await info_plugin.acronyms_add_cmd(dummy_bot, "jid", "nick",
                                       ["foo", "Bar baz"], fake_room_msg, True)
    out = "\n".join(str(x) for x in dummy_bot.replies)
    assert "queued" in out.lower() or "pending" in out.lower()
    with open(str(add), encoding="utf-8") as f:
        rows = [r for r in csv.reader(f)]
        assert any(row[0].lower() == "foo" for row in rows)


@pytest.mark.asyncio
async def test_acronyms_remove_cmd(tmp_slang_files, dummy_bot, fake_room_msg):
    main, _, rem = tmp_slang_files
    with main.open("a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["foo", "to remove"])
    dummy_bot.reset()
    await info_plugin.acronyms_remove_cmd(dummy_bot, "jid", "nick",
                                          ["foo", "to remove"],
                                          fake_room_msg, True)
    out = "\n".join(str(x) for x in dummy_bot.replies)
    assert "queued" in out.lower()
    with open(str(rem), encoding="utf-8") as f:
        rows = [r for r in csv.reader(f)]
        assert any(row[0].lower() == "foo" for row in rows)


@pytest.mark.asyncio
async def test_acronyms_list_cmd(tmp_slang_files, dummy_bot, fake_room_msg):
    _, add, rem = tmp_slang_files
    with open(str(add), "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["foo2", "bar2", "nick1"])
    with open(str(rem), "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["foo2", "bar2", "nick1"])
    dummy_bot.reset()
    await info_plugin.acronyms_list_cmd(dummy_bot, "jid", "nick", [],
                                        fake_room_msg, True)
    out = "\n".join(x[1] for x in dummy_bot.replies)
    assert "Pending Additions" in out or "pending additions" in out
    assert "Pending Removals" in out or "pending removals" in out
    out = "\n".join(x[1] for x in dummy_bot.replies)
    assert "foo2" in out.lower()


@pytest.mark.asyncio
async def test_acronyms_merge_and_delete(monkeypatch, dummy_bot, fake_room_msg,
                                         tmp_slang_files):
    main, add, rem = tmp_slang_files
    with open(str(add), "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["foo", "Bar baz", "testnick"])
    with open(str(rem), "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["lgtm", "Looks good to me", "testnick"])
    dummy_bot.reset()
    await info_plugin.acronyms_merge_cmd(dummy_bot, "jid", "nick", [],
                                         fake_room_msg, True)
    messages = "\n".join(x[1] for x in dummy_bot.replies)
    assert "merged" in messages.lower()
    # Check that addition and removal queues are empty after merge
    assert not add.exists() and not rem.exists()
    with open(str(main), encoding="utf-8") as f:
        all_lines = f.read()
        assert "foo,Bar baz" in all_lines
        assert "lgtm,Looks good to me" not in all_lines


@pytest.mark.asyncio
async def test_acronyms_delete_by_desc_and_nick(tmp_slang_files, dummy_bot,
                                                fake_room_msg):
    _, add, rem = tmp_slang_files
    with open(str(add), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["abcd", "Desc1", "nickA"])
        csv.writer(f).writerow(["abcd", "Desc2", "nickB"])
    with open(str(rem), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["def", "Desc3", "nickA"])
        csv.writer(f).writerow(["def", "Desc4", "nickB"])
    dummy_bot.reset()
    await info_plugin.acronyms_delete_cmd(dummy_bot, "jid", "nick",
                                          ["abcd", "Desc1"],
                                          fake_room_msg, True)
    await info_plugin.acronyms_delete_cmd(dummy_bot, "jid", "nick",
                                          ["def", "Desc3"],
                                          fake_room_msg, True)
    with open(str(add), encoding="utf-8") as f:
        add_rows = list(csv.reader(f))
        assert len(add_rows) == 1 and add_rows[0][2] == "nickB"
    with open(str(rem), encoding="utf-8") as f:
        rem_rows = list(csv.reader(f))
        assert len(rem_rows) == 1 and rem_rows[0][2] == "nickB"
    await info_plugin.acronyms_delete_cmd(dummy_bot, "jid", "nick", ["nickB"],
                                          fake_room_msg, True)
    # Confirm files are now empty (not necessarily deleted)
    assert add.read_text().strip() == "" and rem.read_text().strip() == ""

# ---- WIKIPEDIA ----


@pytest.mark.asyncio
async def test_wikipedia_usage(dummy_bot, fake_room_msg):
    dummy_bot.reset()
    await info_plugin.wikipedia_command(dummy_bot, "jid", "nick", [],
                                        fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "usage" in text.lower()


@pytest.mark.asyncio
async def test_wikipedia_notfound(monkeypatch, dummy_bot, fake_room_msg):
    monkeypatch.setattr(
        info_plugin, "fetch_wikipedia_summary", lambda term: None)
    orig = asyncio.get_event_loop
    monkeypatch.setattr(info_plugin.asyncio, "get_event_loop", lambda: orig())
    await info_plugin.wikipedia_command(dummy_bot, "jid", "nick",
                                        ["somethingunreal"],
                                        fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "No Wikipedia summary found" in text


@pytest.mark.asyncio
async def test_wikipedia_found(monkeypatch, dummy_bot, fake_room_msg):
    monkeypatch.setattr(info_plugin, "fetch_wikipedia_summary", lambda term: (
        "Python", "A summary", "http://wiki/Python"))
    orig = asyncio.get_event_loop
    monkeypatch.setattr(info_plugin.asyncio, "get_event_loop", lambda: orig())
    await info_plugin.wikipedia_command(dummy_bot, "jid", "nick",
                                        ["Python"], fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "Wikipedia" in text and "Python" in text


# ---- INFO ROOM TOGGLE ----

@pytest.mark.asyncio
async def test_information_command_toggle_on(dummy_bot, fake_room_msg):
    await info_plugin.information_command(dummy_bot, "jid", "nick", [],
                                          fake_room_msg, True)
    text = "\n".join(str(x) for x in dummy_bot.replies)
    assert "Usage" in text or "usage" in text
