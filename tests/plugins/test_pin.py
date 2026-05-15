import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, patch
import types

import plugins.pin as pin

@pytest.fixture
def room_jid():
    return 'room@conference.example.com'

@pytest.fixture
def make_msg(room_jid):
    def _make(body="", is_room=True, resource="alice", msg_type="groupchat"):
        class DummyFrom:
            def __init__(self, bare, resource=None):
                self.bare = bare
                self.resource = resource
        msg = {
            "body": body,
            "from": DummyFrom(room_jid if is_room else "alice@example.com", resource),
            "type": msg_type if is_room else "chat",
            "mucnick": resource if is_room else None,
        }
        return msg
    return _make

class DummyBot:
    def __init__(self):
        self.reply = Mock()
        self.db = types.SimpleNamespace(users=Mock())
        self.presence = types.SimpleNamespace()
        self.presence.joined_rooms = {}

@pytest.fixture
def bot():
    b = DummyBot()
    return b

@pytest.fixture(autouse=True)
def clean_caches():
    pin.CACHE_NAMESPACE = "pin-test"  # Avoid interfering with main
    yield
    # Assume that plugins.pin has only in-memory state for test (no real files)

@pytest.mark.asyncio
async def test_pin_command_non_room_add(bot, make_msg, monkeypatch):
    # Should warn to use add as reply/last if not in a room or muc pm
    msg = make_msg(is_room=False)
    await pin.pin_command(bot, "alice@example.com", "Alice", ["add", "last"], msg, False)
    bot.reply.assert_called()
    out = str(bot.reply.call_args[0][1])
    # The actual reply here checks for the string below (per logic)
    assert "only works in rooms" in out

@pytest.mark.asyncio
async def test_pin_command_add_as_reply(monkeypatch, bot, make_msg, room_jid):
    msg = make_msg(body=">quoted\npin add", resource="alice")
    msg["type"] = "groupchat"
    bot.presence.joined_rooms[room_jid] = "alice"
    msg["from"].resource = "alice"
    monkeypatch.setattr(pin, "_is_enabled_for_room", AsyncMock(return_value=True))
    monkeypatch.setattr(pin, "_sender_can_manage_pins_in_room", AsyncMock(return_value=True))
    # Make sure body and quote logic triggers
    monkeypatch.setattr(pin, "extract_reply_quote", lambda body: "hello world" if ">" in body else None)
    monkeypatch.setattr(pin, "_body_without_quote", lambda body: "pin add")
    monkeypatch.setattr(pin, "get_reply_target", lambda msg: "replyid123")
    monkeypatch.setattr(pin, "get_cached_message_by_id", lambda ns, room, rid: {"body": "Cached body", "nick": "bob", "stanza_id": rid})
    monkeypatch.setattr(pin, "_create_pin_entry", AsyncMock(return_value=True))
    # ----------- CRITICALLY PATCH _is_pin_add_command_body to return True -----------
    monkeypatch.setattr(pin, "_is_pin_add_command_body", lambda body: True)
    # and ensure sender present for nick logic
    pin.JOINED_ROOMS[room_jid] = {"nicks": {"alice": {}}}
    handled = await pin._handle_reply_pin_add(bot, msg)
    assert handled is True  # Should call _create_pin_entry

@pytest.mark.asyncio
async def test_pin_command_list_no_pins(bot, make_msg, monkeypatch, room_jid):
    # Pin list when none
    msg = make_msg(is_room=True, body="", resource="alice")
    bot.presence.joined_rooms[room_jid] = "alice"
    monkeypatch.setattr(pin, "_is_enabled_for_room", AsyncMock(return_value=True))
    # Load pin data returns empty
    monkeypatch.setattr(pin, "_load_pin_data", AsyncMock(return_value={}))
    await pin.pin_command(bot, "alice@example.com", "Alice", ["list"], msg, True)
    bot.reply.assert_called()
    args = bot.reply.call_args[0][1]
    assert "No pinned messages" in str(args)

@pytest.mark.asyncio
async def test_pin_command_list_with_pins(bot, make_msg, monkeypatch, room_jid):
    msg = make_msg(is_room=True)
    bot.presence.joined_rooms[room_jid] = "alice"
    monkeypatch.setattr(pin, "_is_enabled_for_room", AsyncMock(return_value=True))
    pin_obj = {
        "id": 1,
        "actor_nick": "alice",
        "created_at": 1234567890,
        "target_nick": "bob",
        "preview": "something cool",
    }
    state = {room_jid: {"pins": [pin_obj]}}
    # Simulate _load_pin_data returning pins in the test room
    monkeypatch.setattr(pin, "_load_pin_data", AsyncMock(return_value=state))
    await pin.pin_command(bot, "alice@example.com", "Alice", ["list"], msg, True)
    args = "\n".join(bot.reply.call_args[0][1]) if isinstance(bot.reply.call_args[0][1], list) else str(bot.reply.call_args[0][1])
    # Accept both Pin # and #1 since the output has "• #1 by alice at ..."
    assert ("#1" in args or "Pin #" in args) and "alice" in args

@pytest.mark.asyncio
async def test_pin_command_show_and_delete(bot, make_msg, monkeypatch, room_jid):
    msg = make_msg(is_room=True)
    bot.presence.joined_rooms[room_jid] = "alice"
    monkeypatch.setattr(pin, "_is_enabled_for_room", AsyncMock(return_value=True))
    pin_obj = {
        "id": 2,
        "actor_nick": "alice",
        "created_at": 1234567890,
        "target_nick": "bob",
        "preview": "hello",
    }
    bucket = {"pins": [pin_obj]}
    state = {room_jid: bucket}
    monkeypatch.setattr(pin, "_load_pin_data", AsyncMock(return_value=state))
    monkeypatch.setattr(pin, "_room_bucket", lambda s, r: s[r])
    # Show
    with patch.object(pin, "_find_pin", return_value=pin_obj):
        await pin.pin_command(bot, "alice@example.com", "Alice", ["show", "2"], msg, True)
        out = "\n".join(bot.reply.call_args[0][1]) if isinstance(bot.reply.call_args[0][1], list) else str(bot.reply.call_args[0][1])
        assert "Pin #2" in out
    # Delete, with permission
    monkeypatch.setattr(pin, "_sender_can_manage_pins_in_room", AsyncMock(return_value=True))
    with patch.object(pin, "_find_pin", return_value=pin_obj):
        with patch.object(pin, "_delete_pin", return_value=True):
            # no-op _save_pin_data
            monkeypatch.setattr(pin, "_save_pin_data", AsyncMock())
            await pin.pin_command(bot, "alice@example.com", "Alice", ["delete", "2"], msg, True)
            args = bot.reply.call_args[0][1]
            assert "Deleted pin" in str(args)

@pytest.mark.asyncio
async def test_pin_command_add_manual_last(bot, make_msg, monkeypatch, room_jid):
    msg = make_msg(is_room=True)
    bot.presence.joined_rooms[room_jid] = "alice"
    monkeypatch.setattr(pin, "_is_enabled_for_room", AsyncMock(return_value=True))
    monkeypatch.setattr(pin, "_sender_can_manage_pins_in_room", AsyncMock(return_value=True))
    # patch _get_recent_target
    pin_obj = {"body": "saved", "nick": "bob", "stanza_id": "stan"}
    monkeypatch.setattr(pin, "_get_recent_target", lambda room, offset=1: pin_obj)
    monkeypatch.setattr(pin, "_create_pin_entry", AsyncMock())
    await pin.pin_command(bot, "alice@example.com", "Alice", ["add", "last"], msg, True)
    bot.reply.assert_not_called()  # Should not reply if create succeeded

def test_trim_and_trim_preview():
    # Simple coverage for _trim and _trim_preview
    assert pin._trim("abc", 10) == "abc"
    assert pin._trim("a" * 10, 5).endswith("…")
    # The _trim_preview code collapses lines and clips at max_chars,
    # so e.g. "hello\nthere\nbye" with 2 lines, 6 chars only shows "hello…"
    assert pin._trim_preview("hello\nthere\nbye", max_lines=2, max_chars=6) == "hello…"

def test_format_pin_line_and_find_delete():
    entry = {
        "id": 1,
        "actor_nick": "alice",
        "created_at": 1234567890,
        "target_nick": "bob",
        "preview": "x" * 12,
    }
    line = pin._format_pin_line(entry)
    assert "alice" in line and "bob" in line
    bucket = {"pins": [entry]}
    assert pin._find_pin(bucket, 1) == entry
    assert pin._delete_pin(bucket, 1)
    assert not pin._find_pin(bucket, 1)

def test_next_free_pin_id_and_generated_text():
    bucket = {"pins": [{"id": 1}, {"id": 2}]}
    assert pin._next_free_pin_id(bucket) == 3
    assert pin._is_pin_generated_text("📌 Pinned message as #1")
    assert not pin._is_pin_generated_text("something else")

def test_format_timestamp_str():
    assert pin._format_timestamp(1234567890).startswith("2009")

def test_room_bucket_new_and_existing():
    state = {}
    room = "abc"
    bucket = pin._room_bucket(state, room)
    assert "pins" in bucket
    bucket["pins"].append({"id": 1})
    bucket2 = pin._room_bucket(state, room)
    assert bucket2 is bucket

def test_is_pin_command_variants():
    pre = pin._prefix()
    assert pin._is_pin_command_message(f"{pre}pin")
    assert pin._is_pin_command_message(f"{pre}pin add")
    assert not pin._is_pin_command_message("notpin")

def test_body_without_quote():
    assert pin._body_without_quote(">hello\nhi\nthere") == "hi\nthere"
    assert pin._body_without_quote(">quoted") == ""

def test__trim_handles_none():
    assert pin._trim(None, 5) == ""
