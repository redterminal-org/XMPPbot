import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import plugins.sed as sed


@pytest.mark.parametrize("text,expect", [
    ("s/foo/bar/", ("foo", "bar", "")),
    ("s#foo#bar#", ("foo", "bar", "")),
    ("s/foo\\/bar/baz/i", ("foo/bar", "baz", "i")),
    ("s/foo/bar/igm", ("foo", "bar", "igm")),
    ("s/foo//", ("foo", "", "")),
    ("s/foo/bar", ("foo", "bar", "")),
    ("z/foo/bar/", (None, None, None)),
    ("s?foo?bar?", (None, None, None)),
    ("s/", (None, None, None)),
])
def test_parse_sed_command(text, expect):
    assert sed.parse_sed_command(text) == expect


@pytest.mark.parametrize("text,expect", [
    (",sed foo bar", ("foo", "bar", "")),
    (",sed 'foo bar' ''", ("foo bar", "", "")),
    (",sed foo bar i", ("foo", "bar", "i")),
    (",sed 'lat(.*)' ''", ("lat(.*)", "", "")),
    (",sed on", (None, None, None)),
    (",sed foo", (None, None, None)),
    (",sed", (None, None, None)),
])
def test_parse_prefixed_sed_command(text, expect):
    with patch.object(sed, 'config', {"prefix": ","}):
        assert sed.parse_prefixed_sed_command(text) == expect


@pytest.mark.parametrize("body,expect", [
    ("s/foo/bar/", ("foo", "bar", "")),
    ("> quoted\ns/foo/bar/i", ("foo", "bar", "i")),  # skips quoted
    ("", (None, None, None)),
    ("not_a_sed", (None, None, None)),
    (",sed foo bar", ("foo", "bar", "")),
])
def test_parse_any_sed_command(body, expect):
    with patch.object(sed, 'config', {"prefix": ","}):
        assert sed.parse_any_sed_command(body) == expect


@pytest.mark.parametrize("body,expect", [
    ("s/foo/bar/", True),
    ("", False),
    ("not a sed", False),
    (",sed foo bar", True),
    (">quote\ns/foo/bar/g", True),
])
def test_is_sed_command(body, expect):
    with patch.object(sed, 'config', {"prefix": ","}):
        assert sed.is_sed_command(body) == expect


def test_read_until_delimiter():
    assert sed.read_until_delimiter(
        "foo/bar/flags", "/") == ("foo", "bar/flags")
    assert sed.read_until_delimiter(
        "foo\\/bar/flags", "/", True) == ("foo/bar", "flags")
    assert sed.read_until_delimiter("foo", "#", require=False) == ("foo", "")


def test_apply_sed_valid_and_invalid():
    # Valid case, ordinary pattern
    out, n = sed.apply_sed("hello foo bar", "foo", "baz", "")
    assert "baz" in out
    assert n == 1

    # global
    out, n = sed.apply_sed("foo foo foo", "foo", "bar", "g")
    assert out.count("bar") == 3
    assert n == 3

    # ignorecase
    out, n = sed.apply_sed("Foo foo", "foo", "bar", "gi")
    assert out.lower().count("bar") == 2

    # literal: regex metachars should be matched as literals
    out, n = sed.apply_sed("1+1", "+", "-", "l")
    assert out == "1-1"

    # non-matching pattern
    out, n = sed.apply_sed("hi", "nope", "something", "")
    assert out == "hi"
    assert n == 0

    # excessively long pattern triggers validation
    out, n = sed.apply_sed("blah", "a"*300, "b", "")
    assert out is None and n == 0

    # invalid regex flags
    out, n = sed.apply_sed("test", "a", "b", "X")  # invalid flag
    assert out is None and n == 0

    # excessive replacement string
    out, n = sed.apply_sed("foo", "foo", "b"*1200, "")
    assert out is None and n == 0

    # input truncation
    inp = "a" * 6000
    out, n = sed.apply_sed(inp, "a", "b", "g")
    assert len(out) <= sed.MAX_INPUT_LENGTH + 10


def test_apply_sed_timeout(monkeypatch):
    # simulate a pattern that hangs (catastrophic backtracking)

    def fake_worker(*_, **__):
        import time
        time.sleep(2)
    monkeypatch.setattr(sed, "_regex_worker", fake_worker)
    # forcibly reduce timeout for the test
    monkeypatch.setattr(sed, "REGEX_TIMEOUT", 0.2)
    out, n = sed.apply_sed("A"*1000, "(A+)+", "B", "g")
    # This triggers the timeout condition, returning (None, -1)
    assert out is None and n == -1


def test_get_last_message_and_get_message_by_id_cache(monkeypatch):
    # monkeypatch get_last_cached_message/get_cached_message_by_id
    room = "room1"
    with patch.object(sed._core, "get_last_cached_message",
                      return_value={"body": "hello"}):
        assert sed.get_last_message(room) == "hello"
    with patch.object(sed._core, "get_last_cached_message",
                      return_value=None):
        assert sed.get_last_message(room) is None
    with patch.object(sed._core, "get_cached_message_by_id",
                      return_value={"body": "foo"}):
        assert sed.get_message_by_id(room, "id123") == "foo"
    with patch.object(sed._core, "get_cached_message_by_id",
                      return_value=None):
        assert sed.get_message_by_id(room, "id123") is None


@pytest.mark.asyncio
async def test_process_sed_correction_reply_and_edge(monkeypatch):
    bot = MagicMock()
    msg = {"body": ">orig\nHello", "from": MagicMock(), "type": "groupchat"}
    is_room = True
    nick = "user1"
    patch_core = patch.object(
        sed._core, "extract_reply_quote", return_value="Hello foo foo")
    patch_msg_by_id = patch.object(
        sed, "get_message_by_id", return_value="Hello foo foo")
    patch_last_message = patch.object(
        sed, "get_last_message", return_value="Hello foo foo")

    # normal flow with quote
    with patch_core, patch_msg_by_id, patch_last_message:
        await sed.process_sed_correction(bot, nick, msg, is_room,
                                         "foo", "bar", "g")
        assert bot.reply.called
        # Should show evidence of replacement
        called_args = bot.reply.call_args[0]
        assert "bar" in called_args[1]

    # pattern not found: should display the correct error message
    with patch.object(sed._core, "extract_reply_quote", return_value=None), \
            patch.object(sed, "get_message_by_id", return_value=None), \
            patch.object(sed, "get_last_message",
                         return_value="no match here"):
        await sed.process_sed_correction(bot, nick, msg, is_room,
                                         "foo", "bar", "g")
        called_args = bot.reply.call_args[0]
        assert "Pattern 'foo' not found in last message" in called_args[1]

    # regex error
    with patch.object(sed, "apply_sed", return_value=(None, 0)):
        await sed.process_sed_correction(bot, nick, msg, is_room,
                                         "(", "b", "")
        called_args = bot.reply.call_args[0]
        assert "Regex error" in called_args[1]

    # regex timeout
    with patch.object(sed, "apply_sed", return_value=(None, -1)):
        await sed.process_sed_correction(bot, nick, msg, is_room,
                                         "foo", "bar", "")
        called_args = bot.reply.call_args[0]
        assert "timeout" in called_args[1].lower()


@pytest.mark.asyncio
async def test_cmd_sed_handler_toggle_and_usage(monkeypatch):
    # covers @command("sed")
    bot = MagicMock()
    msg = {"body": ",sed foo bar", "from": MagicMock(), "type": "groupchat"}
    is_room = True
    args = ["foo", "bar"]

    # handle room_toggle (should return early)
    with patch.object(sed._core, "handle_room_toggle_command",
                      AsyncMock(return_value=True)):
        await sed.cmd_sed_handler(bot, "sender", "nick", args, msg, is_room)
        assert not bot.reply.called  # toggle swallows response

    # Real sed parse
    with patch.object(sed._core, "handle_room_toggle_command",
                      AsyncMock(return_value=False)), \
            patch.object(sed, "parse_prefixed_sed_command",
                         return_value=("foo", "bar", "")), \
            patch.object(sed, "process_sed_correction", AsyncMock()):
        await sed.cmd_sed_handler(bot, "sender", "nick", args, msg, is_room)

    # Usage error
    with patch.object(sed._core, "handle_room_toggle_command",
                      AsyncMock(return_value=False)), \
            patch.object(sed, "parse_prefixed_sed_command",
                         return_value=(None, None, None)):
        await sed.cmd_sed_handler(bot, "sender", "nick", args, msg, is_room)
        assert bot.reply.called
        assert "Usage" in bot.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_on_message_async(monkeypatch):
    bot = MagicMock()
    bot.boundjid = "botjid@muc"
    msg = {
        "body": "s/foo/bar/",
        "from": MagicMock(),
        "type": "groupchat",
        "mucnick": "userX"
    }
    room_name = "room1"

    # Mock enabled_rooms to allow SED feature in room
    async def fake_get_sed_store(bot_self):
        plugin_mock = MagicMock()
        plugin_mock.get_global = AsyncMock(return_value={room_name: True})
        return plugin_mock

    # Patch all the needed underlying helpers
    with \
            patch.object(sed._core, "get_stanza_id", return_value="id456"), \
            patch.object(sed._core, "remember_stanza", return_value=True), \
            patch.object(sed, "get_sed_store", fake_get_sed_store), \
            patch.object(sed, "process_sed_correction",
                         AsyncMock()) as process_correction_mock, \
            patch.object(sed, "parse_any_sed_command",
                         return_value=("foo", "bar", "")), \
            patch.object(sed._core, "cache_message"), \
            patch.object(sed._core, "JOINED_ROOMS",
                         {room_name: {"nick": "botnick"}}):

        msg_room = dict(msg)
        setattr(msg_room["from"], "bare", room_name)
        setattr(msg_room["from"], "resource", "userX")
        await sed.on_message(bot, msg_room)
        assert process_correction_mock.await_count == 1

    # message from bot (should early-exit)
    msg2 = dict(msg)
    msg2["from"] = "botjid@muc"
    with patch.object(sed._core, "get_stanza_id", return_value="id10"):
        bot.boundjid = "botjid@muc"
        # Should return immediately, no crash and no call to process
        await sed.on_message(bot, msg2)


@pytest.mark.asyncio
async def test_on_load_register_events(monkeypatch):
    bot = MagicMock()
    bot.bot_plugins.register_event = MagicMock()
    await sed.on_load(bot)
    assert bot.bot_plugins.register_event.call_count == 2
