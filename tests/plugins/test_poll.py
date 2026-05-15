import pytest
import asyncio
import types
import time

from plugins import poll
from plugins import _core  # for parse_duration, etc.

# --- Dummy infrastructure for bot, rooms, etc. ---

class DummyRoom:
    def __init__(self, bare, resource="alice"):
        self.bare = bare
        self.resource = resource

class DummyMsg(dict):
    def __init__(self, from_bare="room@conf", mucnick="alice", mtype="groupchat", body=None, thread=None, to=None):
        super().__init__()
        self["from"] = DummyRoom(from_bare, mucnick)
        self["type"] = mtype
        self["mucnick"] = mucnick
        self["body"] = body or ""
        self["to"] = to or DummyJID()  # Always set 'to'
        if thread:
            self["thread"] = thread

class DummyJID:
    def __init__(self, bare="bot@server"):
        self.bare = bare
    def __str__(self):
        return self.bare

class DummyStore:
    def __init__(self):
        self._data = {}
    async def get_global(self, key, default=None):
        return self._data.get(key, default)
    async def set_global(self, key, value):
        self._data[key] = value

class DummyPluginDict:
    def __init__(self, extra=None):
        self._plugdict = extra or {}
    def get(self, key, default=None):
        return self._plugdict.get(key, default)
    def __getitem__(self, key):
        return self._plugdict[key]

class DummyBot:
    def __init__(self):
        self._reply_log = []
        self.prefix = ","
        self.version = "1.0"
        self.boundjid = type("J", (), {"bare": "bot@server", "resource": "bot"})()
        self.bot_plugins = types.SimpleNamespace(plugins={})
        self._stores = {"poll": DummyStore()}
        self.db = types.SimpleNamespace(
            users=types.SimpleNamespace(plugin=lambda name: self._stores.setdefault(name, DummyStore())),
        )
        self.plugin = DummyPluginDict()
        # permissions: always role <= USER
        self._user_roles = {}
    async def get_user_role(self, jid, room=None):
        return self._user_roles.get(jid, 80)
    def reply(self, msg, text, mention=None, thread=None, rate_limit=None, ephemeral=None):
        if isinstance(text, (list, tuple)):
            value = "\n".join(str(x) for x in text)
        else:
            value = str(text)
        self._reply_log.append((msg, value))
    def make_message(self, mfrom=None, mto=None, mtype="groupchat", mbody="", **kwargs):
        d = {"from": DummyRoom(mfrom or "room@conf"), "to": mto or DummyJID(), "type": mtype, "body": mbody}
        return d

def public_room_msg(args, nick="alice", body=None):
    if body is None:
        body = " ".join(str(x) for x in args)
    # Always set 'to'
    return DummyMsg(from_bare="room@conf", mucnick=nick, mtype="groupchat", body=body, to=DummyJID())

def _any_line(log, substr):
    for _, txt in log:
        for line in txt.splitlines():
            if substr in line:
                return True
    return False

@pytest.fixture
def dummy_bot():
    bot = DummyBot()
    return bot

async def reset_poll_data(bot):
    poll_store = bot.db.users.plugin("poll")
    await poll_store.set_global("POLL", {"room@conf": True})
    await poll_store.set_global("POLL_DATA", {})
    poll.AUTO_CLOSE_TASKS.clear()

# --- Test helpers/private functions ---

def test__parse_create_args():
    # With duration
    d, q, opts, err = poll._parse_create_args("10m | Q? | A | B | C")
    assert d == 600 and q == "Q?" and opts == ["A", "B", "C"] and err is None
    # No duration
    d, q, opts, err = poll._parse_create_args("Q? | A | B")
    assert d is None and q == "Q?" and opts == ["A", "B"] and err is None
    # Not enough fields
    d, q, opts, err = poll._parse_create_args("one | onlyone")
    assert err
    # Timed but not enough
    d, q, opts, err = poll._parse_create_args("5h | onlyQ | onl")
    assert err
    # Strip/cleanup: this triggers not enough non-empty fields; expect error (not q == "bad" etc.)
    d, q, opts, err = poll._parse_create_args(" | | bad | | again")
    assert err

def test__normalize_poll_roundtrip():
    p = {"id": "5", "question": "Q", "options": ["A"], "votes": {"a":1}, "created_by":"x", "status": "open"}
    norm = poll._normalize_poll("r", "5", dict(p))
    assert int(norm["id"]) == 5
    again = poll._normalize_poll("r", "5", dict(norm))
    assert again == norm

def test__poll_vote_totals_and_winner_summary():
    p = {"options": ["A", "B"], "votes": { "user1": 0, "user2": 1, "user3": 0 }}
    assert poll._poll_vote_totals(p) == [2,1]
    assert poll._winner_summary(p).startswith("Winner: A")
    # Tie
    p2 = {"options": ["A", "B"], "votes": { "user1": 0, "user2": 1 }}
    assert "Tie:" in poll._winner_summary(p2)
    # No votes
    p3 = {"options": ["A", "B"], "votes": {}}
    assert "Winner: none" in poll._winner_summary(p3)

def test__trim_history_limit():
    # Test MAX_HISTORY_PER_ROOM trimming mechanism
    bucket = {"polls": {}}
    now = poll._now()
    # Add 55 closed polls
    for i in range(55):
        bucket["polls"][str(i+1)] = {
            "id": str(i+1), "status": "closed", "created_at": now-200-i, "closed_at": now-100-i
        }
    poll._trim_history(bucket)
    closed = [k for k, p in bucket["polls"].items() if p["status"] == "closed"]
    assert len(closed) == poll.MAX_HISTORY_PER_ROOM

# --- Command coverage (async) ---

@pytest.mark.asyncio
async def test_poll_command_end_to_end(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    poll_store = bot.db.users.plugin("poll")
    await poll_store.set_global("POLL", {"room@conf": True})

    # 1. create poll
    args = ["create", "Choose A or B? | A | B"]
    msg = public_room_msg(args)
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "created")

    # 2. list polls
    args = ["list"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "Open polls")

    # 3. show poll
    args = ["show", "1"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "Poll #1:")

    # 4. show results (no votes yet)
    args = ["result", "1"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "Results:")

    # 5. vote (valid)
    args = ["vote", "1", "2"]
    msg = public_room_msg(args, nick="bob")
    bot._reply_log.clear()
    await poll.poll_command(bot, "bob@svr", "bob", args, msg, True)
    assert _any_line(bot._reply_log, "Your vote for poll #1 is now 'B'")

    # 6. repeat results, (should show vote for B)
    args = ["result", "1"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "bob@svr", "bob", args, msg, True)
    assert "B — 1" in "".join(txt for _, txt in bot._reply_log)

    # 7. vote invalid option (too high)
    args = ["vote", "1", "99"]
    msg = public_room_msg(args, nick="carol")
    bot._reply_log.clear()
    await poll.poll_command(bot, "carol@svr", "carol", args, msg, True)
    assert _any_line(bot._reply_log, "Option must be between")

    # 8. vote: non-existent poll
    args = ["vote", "88", "2"]
    msg = public_room_msg(args, nick="carol")
    bot._reply_log.clear()
    await poll.poll_command(bot, "carol@svr", "carol", args, msg, True)
    assert _any_line(bot._reply_log, "not found")

    # 9. history
    args = ["history"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "Poll history") or _any_line(bot._reply_log, "No poll history")

    # 10. close as poll owner
    args = ["close", "1"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "closed")

    # 11. try vote again after closed
    args = ["vote", "1", "2"]
    msg = public_room_msg(args, nick="carol")
    bot._reply_log.clear()
    await poll.poll_command(bot, "carol@svr", "carol", args, msg, True)
    assert _any_line(bot._reply_log, "is not open")

    # 12. cancel already closed
    args = ["cancel", "1"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert "already" in "".join(txt for _, txt in bot._reply_log)

    # 13. delete poll (after closed)
    args = ["delete", "1"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "deleted")

    # 14. create invalid-long question
    q = "q" * (poll.MAX_QUESTION_LEN+1)
    args = ["create", f"{q} | a | b"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "Question must be between")

    # 15. create too many options
    options = " | ".join([f"o{i}" for i in range(poll.MAX_OPTIONS+1)])
    args = ["create", f"Q | {options}"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "at most")

    # 16. create option too long
    option = "o" * (poll.MAX_OPTION_LEN+1)
    args = ["create", f"Q | a | {option} | b"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    assert _any_line(bot._reply_log, "at most")

    # 17. unknown subcommand
    args = ["XYZZZZZZZ"]
    msg = public_room_msg(args)
    bot._reply_log.clear()
    await poll.poll_command(bot, "bob@svr", "bob", args, msg, True)
    assert _any_line(bot._reply_log, "Unknown")

@pytest.mark.asyncio
async def test_muc_pm_usage(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    poll_store = bot.db.users.plugin("poll")
    await poll_store.set_global("POLL", {"room@conf": True})
    args = ["on"]
    # Simulate muc pm (not groupchat)
    msg = DummyMsg(from_bare="room@conf", mucnick="alice", mtype="chat", body="poll on", to=DummyJID())
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, False)
    # On/off/status are handled, but voting isn't
    args = ["vote", "1", "1"]
    bot._reply_log.clear()
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, False)
    # Should get usage message for poll in PM as only on/off/status supported
    assert _any_line(bot._reply_log, "Use 'poll on/off/status'")

# --- Schedule/auto-close coverage ---

@pytest.mark.asyncio
async def test_poll_auto_close_and_restore(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    poll_store = bot.db.users.plugin("poll")
    room = "room@conf"
    await poll_store.set_global("POLL", {room: True})

    # Create poll with 1-second auto-close
    args = ["create", "1s | Q | A | B"]
    msg = public_room_msg(args)
    await poll.poll_command(bot, "alice@svr", "alice", args, msg, True)
    # Wait for poll to auto-close
    await asyncio.sleep(1.2)

    # Verify poll is closed
    data = await poll._get_data(bot)
    bucket = poll._room_bucket(data, room)
    poll_obj = poll._get_poll(bucket, "1")
    assert poll_obj and poll_obj["status"] != "open"

    # Test schedule/restore cleans up already closed polls
    await poll._restore_auto_close_tasks(bot)
    # No new scheduled tasks for closed poll
    assert not poll.AUTO_CLOSE_TASKS

# --- _can_manage_poll permissions ---

@pytest.mark.asyncio
async def test_can_manage_poll_owner_and_nonowner(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    room = "room@conf"
    poll_store = bot.db.users.plugin("poll")
    await poll_store.set_global("POLL", {room: True})
    msg = public_room_msg([], nick="alice")
    msg["from"].bare = "alice@svr"
    # Setup dummy poll
    poll_obj = {
        "id": 1, "question": "Q", "options": ["A", "B"], "votes": {},
        "created_by": "alice@svr", "status": "open"
    }
    # Direct poll creator
    can = await poll._can_manage_poll(bot, msg, True, poll_obj)
    assert can
    # Fallback: not creator, not moderator/admin, should be False
    poll_obj["created_by"] = "someone@svr"
    can = await poll._can_manage_poll(bot, msg, True, poll_obj)
    assert not can

@pytest.mark.asyncio
async def test_delete_poll_only_when_closed(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    room = "room@conf"
    poll_store = bot.db.users.plugin("poll")
    await poll_store.set_global("POLL", {room: True})
    state = {
        "rooms": {room: {"next_id":2, "polls":{
            "1": {
                "id": 1, "question": "Q", "options": ["A"], "votes": {},
                "created_by": "alice@svr", "status": "open"
            }
        }}}
    }
    await poll_store.set_global("POLL_DATA", state)
    # Trying to delete an open poll should fail
    res, txt = await poll._delete_poll(bot, room, "1")
    assert not res and "still open" in txt
    # Now close and try again
    state["rooms"][room]["polls"]["1"]["status"] = "closed"
    await poll_store.set_global("POLL_DATA", state)
    res, txt = await poll._delete_poll(bot, room, "1")
    assert res and "deleted" in txt

# --- Direct test of _close_poll and error handling

@pytest.mark.asyncio
async def test_close_poll_cancel_and_error(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    room = "room@conf"
    # no poll
    res, txt = await poll._close_poll(bot, room, 99)
    assert not res and "not found" in txt
    # add poll and close
    poll_store = bot.db.users.plugin("poll")
    state = {"rooms": {room: {"next_id":2, "polls": {
        "1": {
            "id": 1, "question": "Q", "options": ["A"], "votes": {},
            "created_by": "alice@svr", "status": "open"
        }
    }}}}
    await poll_store.set_global("POLL_DATA", state)
    # Close poll
    res, txt = await poll._close_poll(bot, room, 1)
    assert res and "closed" in txt
    # Try closing again
    res, txt = await poll._close_poll(bot, room, 1)
    assert not res and "already" in txt
    # Now test cancel path
    # Re-open, cancel poll
    state["rooms"][room]["polls"]["1"]["status"] = "open"
    await poll_store.set_global("POLL_DATA", state)
    res, txt = await poll._close_poll(bot, room, 1, cancelled=True)
    assert res and "cancelled" in txt

# --- plugin load/unload

@pytest.mark.asyncio
async def test_on_load_and_on_unload(dummy_bot):
    await reset_poll_data(dummy_bot)
    bot = dummy_bot
    await poll.on_load(bot)
    await poll.on_unload(bot)
    # Should not crash

# -- Test utils

def test__format_poll_header_and_options_and_results():
    poll_obj = {
        "id": 1, "question": "What?", "options": ["A", "B"], "votes": {"a":0, "b":1},
        "created_by": "alice", "created_by_nick": "Alice",
        "created_at": int(time.time()), "ends_at": None, "status": "open"
    }
    head = poll._format_poll_header(poll_obj)
    options = poll._format_poll_options(poll_obj)
    results = poll._format_poll_results(poll_obj)
    assert "Poll #1" in head
    assert "1. A" in options
    assert "Results:" in results

def test__format_ts_and_remaining():
    now = int(time.time())
    fut = now + 3662
    assert poll._format_ts(now).startswith(str(time.localtime(now).tm_year))
    assert "1h" in poll._format_remaining(fut)
    assert "no limit" in poll._format_remaining(None)
