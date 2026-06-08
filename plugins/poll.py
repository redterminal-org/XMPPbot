"""
Poll plugin with room-local voting, history, and optional timed auto-close.

Features:
- Room-scoped enable/disable/status via MUC DM
- Multiple simultaneous polls per room
- Poll history
- Optional time limits with auto-close
- Voting by real JID (one vote per user per poll, revoting allowed)

Commands:
    {prefix}poll on|off|status
    {prefix}poll create <question> | <option1> | <option2> | option3 ...]
    {prefix}poll create <duration> | <question> | <option1> | <option2> ...]
    {prefix}poll list
    {prefix}poll show <id>
    {prefix}poll result <id>
    {prefix}poll history [limit]
    {prefix}poll vote <id> <option-number>
    {prefix}poll close <id>
    {prefix}poll cancel <id>
    {prefix}poll delete <id>

Examples:
    {prefix}poll create Was essen wir? | Pizza | Döner | Falafel
    {prefix}poll create 1h30m | Was essen wir? | Pizza | Döner | Falafel
    {prefix}poll vote 3 2
    {prefix}poll result 3
"""

import asyncio
import logging
import time

from utils.command import command, Role
from plugins import _core

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "poll",
    "version": "1.1.1",
    "description": "Room polls with voting, history and auto-close",
    "category": "utility",
    "requires": ["rooms", "_core"],
}

POLL_ENABLED_KEY = "POLL"
POLL_DATA_KEY = "POLL_DATA"

MAX_OPTIONS = 10
MAX_QUESTION_LEN = 200
MAX_OPTION_LEN = 100
MAX_HISTORY_PER_ROOM = 50

AUTO_CLOSE_TASKS = {}  # (room_jid, poll_id) -> asyncio.Task


async def get_poll_store(bot):
    return bot.db.users.plugin("poll")


def _poll_reply(bot, msg, text: str):
    bot.reply(msg, text, mention=False, thread=True)


def _system_room_message(room_jid: str) -> dict:
    return {
        "from": type("F", (), {"bare": room_jid})(),
        "type": "groupchat",
    }


def _system_reply(bot, room_jid: str, text: str):
    bot.reply(
        _system_room_message(room_jid),
        text,
        mention=False,
        thread=True,
        rate_limit=False,
        ephemeral=False,
    )


async def _get_data(bot) -> dict:
    store = await get_poll_store(bot)
    data = await store.get_global(POLL_DATA_KEY, default={})
    return data if isinstance(data, dict) else {}


async def _set_data(bot, data: dict):
    store = await get_poll_store(bot)
    await store.set_global(POLL_DATA_KEY, data)


def _room_bucket(data: dict, room_jid: str) -> dict:
    rooms = data.setdefault("rooms", {})
    room = rooms.setdefault(room_jid, {
        "next_id": 1,
        "polls": {},
    })
    room.setdefault("next_id", 1)
    room.setdefault("polls", {})
    return room


def _now() -> int:
    return int(time.time())


def _format_ts(ts: int | None) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _format_remaining(ends_at: int | None) -> str:
    if not ends_at:
        return "no limit"

    remaining = max(0, ends_at - _now())

    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def _parse_create_args(raw: str) -> tuple[int | None, str | None,
                                          list[str] | None, str | None]:
    """
    Parse:
      question | option1 | option2
      30m | question | option1 | option2
      1h30m | question | option1 | option2

    Returns:
      (duration_seconds, question, options, error)
    """
    parts = [p.strip() for p in raw.split("|")]
    parts = [p for p in parts if p]

    if len(parts) < 3:
        out = "Usage: poll create [duration] | question | option1 |"
        out += " option2 [| option3 ...]"
        return None, None, None, out

    duration = _core.parse_duration(parts[0])
    if duration is not None:
        if len(parts) < 4:
            out = "A timed poll needs a question and at least two options."
            return None, None, None, out
        question = parts[1]
        options = parts[2:]
        return duration, question, options, None

    question = parts[0]
    options = parts[1:]
    return None, question, options, None


def _normalize_poll(room_jid: str, poll_id: str, poll: dict) -> dict:
    poll["id"] = int(poll.get("id", int(poll_id)))
    poll["room_jid"] = room_jid
    poll["question"] = str(poll.get("question", "")).strip()
    poll["options"] = [str(o) for o in poll.get("options", [])]
    poll["votes"] = dict(poll.get("votes", {}))
    poll["created_by"] = str(poll.get("created_by", ""))
    poll["created_by_nick"] = str(poll.get("created_by_nick", ""))
    poll["created_at"] = int(poll.get("created_at", _now()))
    poll["ends_at"] = int(poll["ends_at"]) if poll.get("ends_at") else None
    v = int(poll["closed_at"]) if poll.get("closed_at") else None
    poll["closed_at"] = v
    poll["status"] = str(poll.get("status", "open"))
    return poll


def _get_poll(room_bucket: dict, poll_id: str | int) -> dict | None:
    return room_bucket.get("polls", {}).get(str(poll_id))


def _poll_is_open(poll: dict) -> bool:
    return poll.get("status") == "open"


def _poll_vote_totals(poll: dict) -> list[int]:
    totals = [0] * len(poll.get("options", []))
    for _, opt_idx in poll.get("votes", {}).items():
        try:
            idx = int(opt_idx)
        except Exception:
            continue
        if 0 <= idx < len(totals):
            totals[idx] += 1
    return totals


def _winner_summary(poll: dict) -> str:
    totals = _poll_vote_totals(poll)
    total_votes = sum(totals)

    if total_votes == 0:
        return "Winner: none (no votes)"

    best = max(totals)
    winner_indexes = [i for i, count in enumerate(totals) if count == best]

    if len(winner_indexes) == 1:
        idx = winner_indexes[0]
        out = f"Winner: {poll['options'][idx]}"
        out += f" ({best} vote{'s' if best != 1 else ''})"
        return out

    names = ", ".join(poll["options"][i] for i in winner_indexes)
    return f"Tie: {names} ({best} vote{'s' if best != 1 else ''} each)"


def _format_poll_header(poll: dict) -> str:
    status = poll.get("status", "unknown")
    limit = _format_ts(poll.get("ends_at")) if poll.get(
        "ends_at") else "no limit"
    v = f"{poll.get('created_by_nick') or poll.get('created_by') or 'unknown'}"
    return (
        f"📊 Poll #{poll['id']}: {poll['question']}\n"
        f"Status: {status}\n"
        f"Created by: {v}\n"
        f"Created at: {_format_ts(poll.get('created_at'))}\n"
        f"Ends at: {limit}"
    )


def _format_poll_options(poll: dict) -> str:
    lines = []
    for i, option in enumerate(poll.get("options", []), 1):
        lines.append(f"{i}. {option}")
    return "\n".join(lines)


def _format_poll_results(poll: dict) -> str:
    totals = _poll_vote_totals(poll)
    total_votes = sum(totals)

    lines = [_format_poll_header(poll), "", "Results:"]
    for i, option in enumerate(poll.get("options", []), 1):
        count = totals[i - 1]
        label = "vote" if count == 1 else "votes"
        lines.append(f"{i}. {option} — {count} {label}")

    lines.append("")
    lines.append(f"Total votes: {total_votes}")
    lines.append(_winner_summary(poll))
    return "\n".join(lines)


def _trim_history(room_bucket: dict):
    polls = room_bucket.get("polls", {})
    closed = [
        (pid, poll)
        for pid, poll in polls.items()
        if poll.get("status") in {"closed", "cancelled"}
    ]

    if len(closed) <= MAX_HISTORY_PER_ROOM:
        return

    closed.sort(
        key=lambda item: (
            int(item[1].get("closed_at") or item[1].get("created_at") or 0),
            int(item[0]),
        )
    )

    to_remove = len(closed) - MAX_HISTORY_PER_ROOM
    for pid, _ in closed[:to_remove]:
        polls.pop(pid, None)


async def _can_manage_poll(bot, msg, is_room: bool, poll: dict) -> bool:
    sender_jid, _, _ = await _core.get_real_jid(bot, msg)
    if sender_jid and sender_jid == poll.get("created_by"):
        return True

    if not _core._is_public_muc(msg, is_room):
        return False

    room_jid = msg["from"].bare
    nick = msg.get("mucnick") or msg["from"].resource or ""
    return await _core.is_room_moderator_or_admin(bot, room_jid, str(nick))


async def _close_poll(bot, room_jid: str, poll_id: str | int, *,
                      cancelled=False, announce=True) -> tuple[bool, str]:
    data = await _get_data(bot)
    room = _room_bucket(data, room_jid)
    poll = _get_poll(room, poll_id)

    if not poll:
        return False, f"Poll #{poll_id} not found."

    poll = _normalize_poll(room_jid, str(poll_id), poll)

    if poll["status"] != "open":
        return False, f"Poll #{poll_id} is already {poll['status']}."

    poll["status"] = "cancelled" if cancelled else "closed"
    poll["closed_at"] = _now()
    room["polls"][str(poll_id)] = poll
    _trim_history(room)
    await _set_data(bot, data)

    task = AUTO_CLOSE_TASKS.pop((room_jid, str(poll_id)), None)
    if task:
        task.cancel()

    if announce:
        if cancelled:
            _system_reply(
                bot,
                room_jid,
                f"🛑 Poll #{poll['id']} was cancelled.\n{poll['question']}",
            )
        else:
            _system_reply(
                bot,
                room_jid,
                f"⏰ Poll #{poll['id']} was closed.\n\n"
                f"{_format_poll_results(poll)}",
            )

    return (True,
            f"Poll #{poll['id']} {'cancelled' if cancelled else 'closed'}.")


async def _delete_poll(bot, room_jid: str,
                       poll_id: str | int) -> tuple[bool, str]:
    data = await _get_data(bot)
    room = _room_bucket(data, room_jid)
    poll = _get_poll(room, poll_id)

    if not poll:
        return False, f"Poll #{poll_id} not found."

    poll = _normalize_poll(room_jid, str(poll_id), poll)

    if poll["status"] == "open":
        return (False,
                f"Poll #{poll_id} is still open. Close or cancel it first.")

    room["polls"].pop(str(poll_id), None)
    await _set_data(bot, data)

    return True, f"Poll #{poll['id']} deleted from history."


async def _auto_close_after(bot, room_jid: str, poll_id: str, delay: int):
    key = (room_jid, poll_id)
    try:
        await asyncio.sleep(delay)
        await _close_poll(bot, room_jid, poll_id, cancelled=False,
                          announce=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[POLL] Failed to auto-close poll #%s in %s",
                      poll_id, room_jid)
    finally:
        AUTO_CLOSE_TASKS.pop(key, None)


def _schedule_auto_close(bot, room_jid: str, poll: dict):
    poll_id = str(poll["id"])
    key = (room_jid, poll_id)

    old = AUTO_CLOSE_TASKS.pop(key, None)
    if old:
        old.cancel()

    ends_at = poll.get("ends_at")
    if not ends_at or poll.get("status") != "open":
        return

    delay = max(0, int(ends_at) - _now())
    AUTO_CLOSE_TASKS[key] = asyncio.create_task(_auto_close_after(bot,
                                                                  room_jid,
                                                                  poll_id,
                                                                  delay))


async def _restore_auto_close_tasks(bot):
    data = await _get_data(bot)
    rooms = data.get("rooms", {})

    for room_jid, room in rooms.items():
        polls = room.get("polls", {})
        for poll_id, raw_poll in polls.items():
            poll = _normalize_poll(room_jid, poll_id, raw_poll)
            room["polls"][poll_id] = poll

            if poll["status"] != "open":
                continue

            ends_at = poll.get("ends_at")
            if not ends_at:
                continue

            if ends_at <= _now():
                await _close_poll(bot, room_jid, poll_id, cancelled=False,
                                  announce=True)
                continue

            _schedule_auto_close(bot, room_jid, poll)

    await _set_data(bot, data)


async def _poll_handle_create(bot, sender_jid, nick, msg, room_jid,
                              data, room, args):
    raw = " ".join(args[1:]).strip()
    duration, question, options, error = _parse_create_args(raw)
    if error:
        _poll_reply(bot, msg, error)
        return

    question = str(question or "").strip()
    options = [str(o).strip() for o in (options or []) if str(o).strip()]

    if len(question) == 0 or len(question) > MAX_QUESTION_LEN:
        _poll_reply(
            bot,
            msg,
            "❌ Question must be between 1 and"
            f" {MAX_QUESTION_LEN} characters.",
        )
        return

    if len(options) < 2:
        _poll_reply(bot, msg, "❌ A poll needs at least two options.")
        return

    if len(options) > MAX_OPTIONS:
        _poll_reply(
            bot,
            msg,
            f"❌ A poll can have at most {MAX_OPTIONS}"
            " options.",
        )
        return

    if any(len(opt) > MAX_OPTION_LEN for opt in options):
        _poll_reply(
            bot,
            msg,
            f"❌ Each option must be at most {MAX_OPTION_LEN}"
            " characters long.",
        )
        return

    poll_id = room["next_id"]
    room["next_id"] += 1

    creator_jid, _, _ = await _core.get_real_jid(bot, msg)
    creator_jid = creator_jid or str(sender_jid)
    creator_nick = (msg.get("mucnick") or msg["from"].resource
                    or nick or str(sender_jid))

    poll = {
        "id": poll_id,
        "room_jid": room_jid,
        "question": question,
        "options": options,
        "votes": {},
        "created_by": creator_jid,
        "created_by_nick": creator_nick,
        "created_at": _now(),
        "ends_at": (_now() + duration) if duration else None,
        "closed_at": None,
        "status": "open",
    }

    room["polls"][str(poll_id)] = poll
    await _set_data(bot, data)

    if poll.get("ends_at"):
        _schedule_auto_close(bot, room_jid, poll)

    lines = [
        f"📊 Poll #{poll_id} created: {question}",
        "",
        _format_poll_options(poll),
        "",
        f"Vote with: {bot.prefix}poll vote {poll_id} <number>",
    ]
    if poll.get("ends_at"):
        v = f"{_format_ts(poll['ends_at'])}"
        v += f" ({_format_remaining(poll['ends_at'])})"
        lines.append(f"Auto-close: {v}")

    _poll_reply(bot, msg, "\n".join(lines))


async def _poll_handle_list(bot, msg, room_jid, room):
    open_polls = []
    for poll_id, raw_poll in room.get("polls", {}).items():
        poll = _normalize_poll(room_jid, poll_id, raw_poll)
        if poll["status"] == "open":
            open_polls.append(poll)

    if not open_polls:
        _poll_reply(bot, msg, "ℹ️ No open polls in this room.")
        return

    open_polls.sort(key=lambda p: p["id"])
    lines = ["📋 Open polls in this room:"]
    for poll in open_polls:
        if poll.get("ends_at"):
            suffix = f", ends {_format_ts(poll['ends_at'])}"
        else:
            suffix = ""
        lines.append(
            f"#{poll['id']} {poll['question']}"
            f" ({len(poll['options'])} options{suffix})"
        )

    _poll_reply(bot, msg, "\n".join(lines))


async def _poll_handle_history(bot, msg, room_jid, room, args):
    limit = 10
    if len(args) > 1 and str(args[1]).isdigit():
        limit = max(1, min(50, int(args[1])))

    closed_polls = []
    for poll_id, raw_poll in room.get("polls", {}).items():
        poll = _normalize_poll(room_jid, poll_id, raw_poll)
        if poll["status"] in {"closed", "cancelled"}:
            closed_polls.append(poll)

    if not closed_polls:
        _poll_reply(bot, msg, "ℹ️ No poll history for this room.")
        return

    closed_polls.sort(
        key=lambda p: (-int(p.get("closed_at")
                            or p.get("created_at") or 0), -p["id"])
    )
    lines = ["🗂️ Poll history:"]
    for poll in closed_polls[:limit]:
        lines.append(f"#{poll['id']} [{poll['status']}] {poll['question']}")

    _poll_reply(bot, msg, "\n".join(lines))


async def _poll_handle_show_result(bot, msg, room_jid, room, args, sub):
    if len(args) < 2 or not str(args[1]).isdigit():
        _poll_reply(bot, msg, f"Usage: {bot.prefix}poll {sub} <id>")
        return

    poll = _get_poll(room, args[1])
    if not poll:
        _poll_reply(bot, msg, f"❌ Poll #{args[1]} not found.")
        return

    poll = _normalize_poll(room_jid, args[1], poll)

    if sub == "show":
        lines = [
            _format_poll_header(poll),
            "",
            _format_poll_options(poll),
        ]
        if poll["status"] == "open":
            lines += [
                "",
                f"Vote with: {bot.prefix}poll"
                f" vote {poll['id']} <number>",
            ]
        _poll_reply(bot, msg, "\n".join(lines))
        return

    _poll_reply(bot, msg, _format_poll_results(poll))


async def _poll_handle_vote(bot, msg, room_jid, room, data, args):
    if (len(args) != 3 or not str(args[1]).isdigit()
            or not str(args[2]).isdigit()):
        _poll_reply(
            bot,
            msg,
            f"Usage: {bot.prefix}poll vote <id> <option-number>",
        )
        return

    poll_id = args[1]
    option_num = int(args[2])

    poll = _get_poll(room, poll_id)
    if not poll:
        _poll_reply(bot, msg, f"❌ Poll #{poll_id} not found.")
        return

    poll = _normalize_poll(room_jid, poll_id, poll)

    if not _poll_is_open(poll):
        _poll_reply(bot, msg, f"❌ Poll #{poll_id} is not open.")
        return

    if option_num < 1 or option_num > len(poll["options"]):
        _poll_reply(
            bot,
            msg,
            "❌ Option must be between 1 and"
            f" {len(poll['options'])}.",
        )
        return

    voter_jid, _, _ = await _core.get_real_jid(bot, msg)
    if not voter_jid:
        _poll_reply(
            bot,
            msg,
            "❌ Could not determine your real JID"
            " in this room.",
        )
        return

    poll["votes"][voter_jid] = option_num - 1
    room["polls"][str(poll_id)] = poll
    await _set_data(bot, data)

    _poll_reply(
        bot,
        msg,
        f"✅ Your vote for poll #{poll_id}"
        f" is now '{poll['options'][option_num - 1]}'.",
    )


async def _poll_handle_manage(bot, msg, room_jid, room, is_room, args, sub):
    if len(args) < 2 or not str(args[1]).isdigit():
        _poll_reply(bot, msg, f"Usage: {bot.prefix}poll {sub} <id>")
        return

    poll_id = args[1]
    poll = _get_poll(room, poll_id)
    if not poll:
        _poll_reply(bot, msg, f"❌ Poll #{poll_id} not found.")
        return

    poll = _normalize_poll(room_jid, poll_id, poll)

    if not await _can_manage_poll(bot, msg, is_room, poll):
        _poll_reply(
            bot,
            msg,
            "⛔ Only the poll creator or a room"
            " moderator/admin can do that.",
        )
        return

    if sub == "delete":
        success, text = await _delete_poll(bot, room_jid, poll_id)
        _poll_reply(bot, msg, ("✅ " if success else "❌ ") + text)
        return

    success, text = await _close_poll(
        bot,
        room_jid,
        poll_id,
        cancelled=(sub == "cancel"),
        announce=True,
    )
    _poll_reply(bot, msg, ("✅ " if success else "❌ ") + text)


@command("poll", role=Role.USER)
async def poll_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Poll control and voting.

    Usage:
        {prefix}poll on
        {prefix}poll off
        {prefix}poll status
        {prefix}poll create [duration] | question | option1 | option2 | ...]
        {prefix}poll list
        {prefix}poll show <id>
        {prefix}poll result <id>
        {prefix}poll history [limit]
        {prefix}poll vote <id> <option-number>
        {prefix}poll close <id>
        {prefix}poll cancel <id>
        {prefix}poll delete <id>
    """
    handled = await _core.handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_poll_store,
        key=POLL_ENABLED_KEY,
        label="Polls",
        storage="dict",
        log_prefix="[POLL]",
    )
    if handled:
        return

    if not args:
        _poll_reply(
            bot,
            msg,
            f"Usage: {bot.prefix}poll <on|off|status|create|list|show|result|"
            "history|vote|close|cancel|delete>",
        )
        return

    sub = args[0].lower()
    is_muc_pm = _core._is_muc_pm(msg)
    is_public_room = _core._is_public_muc(msg, is_room)

    if is_public_room:
        room_jid = msg["from"].bare

        if not await _core._is_enabled_for_room(bot, POLL_ENABLED_KEY,
                                                "poll", room_jid):
            return

        data = await _get_data(bot)
        room = _room_bucket(data, room_jid)

        if sub == "create":
            await _poll_handle_create(bot, sender_jid, nick, msg,
                                      room_jid, data, room, args)
            return

        if sub == "list":
            await _poll_handle_list(bot, msg, room_jid, room)
            return

        if sub == "history":
            await _poll_handle_history(bot, msg, room_jid, room, args)
            return

        if sub in {"show", "result"}:
            await _poll_handle_show_result(bot, msg, room_jid, room,
                                           args, sub)
            return

        if sub == "vote":
            await _poll_handle_vote(bot, msg, room_jid, room, data, args)
            return

        if sub in {"close", "cancel", "delete"}:
            await _poll_handle_manage(bot, msg, room_jid, room, is_room,
                                      args, sub)
            return

        _poll_reply(
            bot,
            msg,
            f"❌ Unknown poll subcommand. Use {bot.prefix}poll"
            " list|show|result|history|vote|create|close|cancel|delete",
        )
        return

    if is_muc_pm:
        _poll_reply(
            bot,
            msg,
            "ℹ️ Use 'poll on/off/status' here. Create, vote and manage polls"
            "in the public room.",
        )
        return


async def on_load(bot):
    log.info("[POLL] Plugin loading...")
    await _restore_auto_close_tasks(bot)
    log.info("[POLL] Plugin loaded")


async def on_unload(bot):
    for task in list(AUTO_CLOSE_TASKS.values()):
        task.cancel()
    AUTO_CLOSE_TASKS.clear()
    log.info("[POLL] Plugin unloaded")
