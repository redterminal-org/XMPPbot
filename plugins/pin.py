"""
Pin messages in a room and list/show/delete stored pins.

Usage
-----
Reply to a room message and send:
    {prefix}pin add

For clients without reply support:
    {prefix}pin add last
    {prefix}pin add last <n>

Manage pins:
    {prefix}pin list [page]
    {prefix}pin show <id>
    {prefix}pin delete <id>

Room control (MUC PM):
    {prefix}pin on|off|status
"""

from __future__ import annotations

import html
import logging
import time
from functools import partial
from typing import Any

from utils.command import command, Role
from utils.config import config
from plugins import _core
from plugins.rooms import JOINED_ROOMS

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "pin",
    "version": "1.2.0",
    "description": "Pin room messages with paging and non-reply fallback.",
    "category": "utility",
    "requires": ["rooms", "_core"],
}

PIN_ENABLED_KEY = "PIN"
PIN_DATA_KEY = "PIN_DATA"

PINS_FIELD = "pins"
PAGE_SIZE = 10
CACHE_NAMESPACE = "pin"


async def get_pin_store(bot):
    return bot.db.users.plugin("pin")


def _prefix() -> str:
    return config.get("prefix", ",")


def _trim(text: str | None, limit: int = 700) -> str:
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _trim_preview(text: str | None, max_lines: int = 1, max_chars: int = 240) -> str:
    if not text:
        return ""

    lines = str(text).strip().splitlines()
    clipped_lines = lines[:max_lines]
    clipped = "\n".join(clipped_lines).strip()

    if len(lines) > max_lines:
        clipped += " …"

    if len(clipped) <= max_chars:
        return clipped

    return clipped[: max_chars - 1].rstrip() + "…"


def _is_pin_generated_text(text: str | None) -> bool:
    if not text:
        return False

    stripped = str(text).strip()

    return (
        stripped.startswith("📌 Pinned message as #")
        or stripped.startswith("📌 Pins for ")
        or stripped.startswith("📌 Pin #")
    )


def _room_key_from_msg(msg, is_room: bool) -> str | None:
    if is_room:
        try:
            return str(msg["from"].bare)
        except Exception:
            return None

    try:
        room = str(msg["from"].bare)
        if room in JOINED_ROOMS:
            return room
    except Exception:
        pass

    return None


def _body_without_quote(body: str) -> str:
    if not body:
        return ""

    lines = body.splitlines()
    idx = 0

    while idx < len(lines) and lines[idx].startswith(">"):
        idx += 1

    while idx < len(lines) and not lines[idx].strip():
        idx += 1

    return "\n".join(lines[idx:]).strip()


def _safe_get_sender_nick(msg) -> str | None:
    try:
        return str(msg.get("mucnick") or msg["from"].resource or "")
    except Exception:
        return None


def _safe_get_sender_jid(msg, fallback=None) -> str | None:
    try:
        value = getattr(msg["from"], "bare", None)
        if value:
            return str(value)
    except Exception:
        pass

    if fallback is not None:
        try:
            return str(fallback)
        except Exception:
            return None

    return None


def _normalize_pin_data(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}

    normalized = {}

    for room, room_data in state.items():
        if not isinstance(room_data, dict):
            room_data = {}

        pins = room_data.get(PINS_FIELD, [])
        if not isinstance(pins, list):
            pins = []

        normalized[str(room)] = {
            PINS_FIELD: pins,
        }

    return normalized


async def _load_pin_data(bot) -> dict[str, Any]:
    store = await get_pin_store(bot)
    state = await store.get_global(PIN_DATA_KEY, default={})
    return _normalize_pin_data(state)


async def _save_pin_data(bot, state: dict[str, Any]) -> None:
    store = await get_pin_store(bot)
    await store.set_global(PIN_DATA_KEY, state)


def _room_bucket(state: dict[str, Any], room: str) -> dict[str, Any]:
    if room not in state:
        state[room] = {
            PINS_FIELD: [],
        }
    return state[room]


async def _enabled_rooms(bot) -> dict[str, bool]:
    store = await get_pin_store(bot)
    state = await store.get_global(PIN_ENABLED_KEY, default={})
    if not isinstance(state, dict):
        return {}
    return state


async def _is_enabled_for_room(bot, room_jid: str) -> bool:
    enabled = await _enabled_rooms(bot)
    return bool(enabled.get(room_jid))


async def _sender_can_manage_pins_in_room(bot, msg, room_jid: str) -> bool:
    """
    True if sender is moderator/admin/owner in this room (affiliation or role fallback).
    """
    nick = msg.get("mucnick") or msg["from"].resource or ""
    return await _core.is_room_moderator_or_admin(bot, room_jid, str(nick))


def _format_timestamp(ts: int | float | None) -> str:
    if not ts:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(ts)))
    except Exception:
        return "unknown"


def _format_pin_line(entry: dict[str, Any]) -> str:
    pin_id = entry.get("id", "?")
    actor_nick = entry.get("actor_nick") or "unknown"
    created_at = _format_timestamp(entry.get("created_at"))
    target_nick = entry.get("target_nick") or "unknown"
    preview = _trim_preview(entry.get("preview") or entry.get("target_text") or "—", max_lines=1, max_chars=240)
    return f"• #{pin_id} by {actor_nick} at {created_at} | target: {target_nick} | {preview}"


def _find_pin(bucket: dict[str, Any], pin_id: int) -> dict[str, Any] | None:
    for entry in bucket.get(PINS_FIELD, []):
        try:
            if int(entry.get("id")) == pin_id:
                return entry
        except Exception:
            continue
    return None


def _delete_pin(bucket: dict[str, Any], pin_id: int) -> bool:
    pins = bucket.get(PINS_FIELD, [])
    for idx, entry in enumerate(pins):
        try:
            if int(entry.get("id")) == pin_id:
                pins.pop(idx)
                return True
        except Exception:
            continue
    return False


def _next_free_pin_id(bucket: dict[str, Any]) -> int:
    used_ids = set()

    for entry in bucket.get(PINS_FIELD, []):
        try:
            used_ids.add(int(entry.get("id")))
        except Exception:
            continue

    pin_id = 1
    while pin_id in used_ids:
        pin_id += 1

    return pin_id


def _is_pin_command_message(body: str) -> bool:
    prefix = _prefix()
    stripped = body.strip().lower()
    return stripped == f"{prefix}pin" or stripped.startswith(f"{prefix}pin ")


def _is_pin_add_command_body(body: str) -> bool:
    prefix = _prefix()
    stripped = body.strip().lower()
    return stripped == f"{prefix}pin add"


def _recent_cache_entries(room: str) -> list[dict[str, Any]]:
    return _core.get_cached_messages(CACHE_NAMESPACE, room)


def _get_recent_target(room: str, offset: int = 1) -> dict[str, Any] | None:
    if offset < 1:
        return None

    entries = _recent_cache_entries(room)
    if not entries:
        return None

    filtered: list[dict[str, Any]] = []
    for entry in entries:
        body = entry.get("body") or ""
        if not body.strip():
            continue
        if _is_pin_command_message(body):
            continue
        if _is_pin_generated_text(body):
            continue
        filtered.append(entry)

    if not filtered:
        return None

    if offset > len(filtered):
        return None

    return filtered[-offset]


async def _create_pin_entry(
    bot,
    msg,
    room: str,
    sender_jid,
    nick,
    target_text: str,
    target_nick: str,
    target_stanza_id: str | None,
    reply_id: str | None,
    quote_text: str | None,
    cmd_body: str,
    source: str,
):
    if not target_text:
        bot.reply(msg, "❌ Could not resolve the target message.")
        return True

    if _is_pin_generated_text(target_text):
        bot.reply(msg, "❌ Pin-generated bot messages cannot be pinned.")
        return True

    state = await _load_pin_data(bot)
    bucket = _room_bucket(state, room)

    pin_id = _next_free_pin_id(bucket)
    sender_nick = _safe_get_sender_nick(msg) or nick or "unknown"
    sender_real_jid = _safe_get_sender_jid(msg, fallback=sender_jid)

    preview_source = target_text or quote_text or "target message"
    preview = _trim_preview(
        html.unescape(str(preview_source).replace("\xa0", " ")),
        max_lines=2,
        max_chars=240,
    )

    entry = {
        "id": pin_id,
        "room": room,
        "target_room": room,
        "created_at": int(time.time()),
        "actor_nick": str(sender_nick),
        "actor_jid": sender_real_jid,
        "reply_id": str(reply_id) if reply_id else None,
        "target_reply_to": str(reply_id) if reply_id else None,
        "target_stanza_id": target_stanza_id,
        "target_nick": target_nick,
        "target_text": _trim(target_text, 4000) if target_text else None,
        "preview": preview,
        "pin_command_body": _trim(cmd_body, 500),
        "source": source,
        "client_quote_available": bool(quote_text),
        "raw_body_excerpt": _trim(msg.get("body", "") or "", 1000),
        "pinned_via": "pin_command",
    }

    bucket[PINS_FIELD].append(entry)
    await _save_pin_data(bot, state)

    bot.reply(
        msg,
        [
            f"📌 Pinned message as #{entry['id']}.",
            f"Source: {entry['source']}",
            f"Reply target id: {reply_id or 'none'}",
            f"Target nick: {target_nick}",
            f"Preview: {preview}",
        ],
        mention=False,
    )
    return True


async def _handle_reply_pin_add(bot, msg):
    try:
        body = msg.get("body", "") or ""
        if not body.strip():
            return False

        if msg.get("type") != "groupchat":
            return False

        room = str(msg["from"].bare)
        if room not in JOINED_ROOMS:
            return False

        if not await _is_enabled_for_room(bot, room):
            return False

        # permission guard for reply-based pin add
        if not await _sender_can_manage_pins_in_room(bot, msg, room):
            return False

        quote_text = _core.extract_reply_quote(body)
        if not quote_text:
            return False

        cmd_body = _body_without_quote(body)
        if not _is_pin_add_command_body(cmd_body):
            return False

        reply_id = _core.get_reply_target(msg)
        cached_entry = None
        target_text = None
        target_nick = "unknown"
        target_stanza_id = None
        source = "quote"

        if reply_id:
            cached_entry = _core.get_cached_message_by_id(CACHE_NAMESPACE, room, reply_id)
            if cached_entry:
                target_text = cached_entry.get("body")
                target_nick = cached_entry.get("nick") or "unknown"
                target_stanza_id = cached_entry.get("stanza_id") or str(reply_id)
                source = "reply-cache"

        if not target_text and quote_text:
            target_text = quote_text
            target_stanza_id = str(reply_id) if reply_id else None
            source = "quote"

        return await _create_pin_entry(
            bot=bot,
            msg=msg,
            room=room,
            sender_jid=msg["from"],
            nick=_safe_get_sender_nick(msg),
            target_text=target_text,
            target_nick=target_nick,
            target_stanza_id=target_stanza_id,
            reply_id=reply_id,
            quote_text=quote_text,
            cmd_body=cmd_body,
            source=source,
        )
    except Exception:
        log.exception("[PIN] Error handling reply-based pin add")
        return False


async def _on_groupchat_message(bot, msg):
    try:
        if await _handle_reply_pin_add(bot, msg):
            return

        body = msg.get("body", "").strip()
        if not body:
            return

        if msg.get("type") != "groupchat":
            return

        room = str(msg["from"].bare)
        if room not in JOINED_ROOMS:
            return

        stanza_id = _core.get_stanza_id(msg)
        if not _core.remember_stanza(CACHE_NAMESPACE, stanza_id):
            return

        if _is_pin_command_message(body):
            return

        actor_nick = msg.get("mucnick") or msg["from"].resource or "unknown"
        _core.cache_message(
            CACHE_NAMESPACE,
            room,
            actor_nick,
            body,
            stanza_id,
            maxlen=80,
            extra={"ts": int(time.time())},
        )

    except Exception:
        log.exception("[PIN] Error in groupchat message cache handler")


@command("pin", role=Role.USER)
async def pin_command(bot, sender_jid, nick, args, msg, is_room):
    if not args:
        bot.reply(
            msg,
            (
                f"Usage: {_prefix()}pin add [last [n]] | {_prefix()}pin list [page] | "
                f"{_prefix()}pin show <id> | "
                f"{_prefix()}pin delete <id> | {_prefix()}pin on|off|status"
            ),
            mention=False,
        )
        return

    handled = await _core.handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_pin_store,
        key=PIN_ENABLED_KEY,
        label="Pin plugin",
        storage="dict",
        log_prefix="[PIN]",
    )
    if handled:
        return

    room = _room_key_from_msg(msg, is_room)
    if not room:
        bot.reply(msg, "ℹ️ This command only works in rooms or MUC private messages.")
        return

    subcmd = str(args[0]).lower()

    if subcmd == "list":
        if not await _is_enabled_for_room(bot, room):
            bot.reply(msg, "ℹ️ Pin plugin is disabled in this room.")
            return

        page = 1
        if len(args) >= 2:
            try:
                page = max(1, int(args[1]))
            except ValueError:
                bot.reply(msg, f"❌ Usage: {_prefix()}pin list [page]")
                return

        state = await _load_pin_data(bot)
        bucket = _room_bucket(state, room)
        pins = list(bucket.get(PINS_FIELD, []))
        pins.sort(key=lambda x: int(x.get("id", 0)), reverse=True)

        if not pins:
            bot.reply(msg, "📌 No pinned messages stored for this room.", mention=False)
            return

        page_items, page, total_pages, total = _core.paginate_items(pins, page, PAGE_SIZE)

        lines = [f"📌 Pins for {room} ({total}) - Page {page}/{total_pages}", ""]
        lines.extend(_format_pin_line(entry) for entry in page_items)

        if page < total_pages:
            lines.append("")
            lines.append(f"Use {_prefix()}pin list {page + 1} for the next page.")

        bot.reply(msg, lines, mention=False)
        return

    if subcmd == "show":
        if not await _is_enabled_for_room(bot, room):
            bot.reply(msg, "ℹ️ Pin plugin is disabled in this room.")
            return

        if len(args) < 2:
            bot.reply(msg, f"❌ Usage: {_prefix()}pin show <id>")
            return

        try:
            pin_id = int(args[1])
        except ValueError:
            bot.reply(msg, f"❌ Usage: {_prefix()}pin show <id>")
            return

        state = await _load_pin_data(bot)
        bucket = _room_bucket(state, room)
        entry = _find_pin(bucket, pin_id)

        if not entry:
            bot.reply(msg, f"❌ Pin #{pin_id} not found in this room.")
            return

        lines = [
            f"📌 Pin #{entry.get('id')}",
            f"Room: {entry.get('room') or room}",
            f"Created: {_format_timestamp(entry.get('created_at'))}",
            f"Pinned by: {entry.get('actor_nick') or 'unknown'} ({entry.get('actor_jid') or 'unknown'})",
            f"Target nick: {entry.get('target_nick') or 'unknown'}",
            f"Reply target id: {entry.get('reply_id') or 'unknown'}",
            f"Target stanza id: {entry.get('target_stanza_id') or 'unknown'}",
            f"Source: {entry.get('source') or 'unknown'}",
        ]

        preview = entry.get("preview")
        if preview:
            lines.extend(["", "Preview:", preview])

        full_text = entry.get("target_text")
        if full_text:
            lines.extend(["", "Pinned text:", full_text])

        bot.reply(msg, lines, mention=False)
        return

    if subcmd == "delete":
        if not await _is_enabled_for_room(bot, room):
            bot.reply(msg, "ℹ️ Pin plugin is disabled in this room.")
            return

        # permission guard
        if not await _sender_can_manage_pins_in_room(bot, msg, room):
            bot.reply(msg, "⛔ Only room moderators/admins/owners can delete pins.", mention=False)
            return

        if len(args) < 2:
            bot.reply(msg, f"❌ Usage: {_prefix()}pin delete <id>")
            return

        try:
            pin_id = int(args[1])
        except ValueError:
            bot.reply(msg, f"❌ Usage: {_prefix()}pin delete <id>")
            return

        state = await _load_pin_data(bot)
        bucket = _room_bucket(state, room)

        if not _delete_pin(bucket, pin_id):
            bot.reply(msg, f"❌ Pin #{pin_id} not found in this room.")
            return

        await _save_pin_data(bot, state)
        bot.reply(msg, f"✅ Deleted pin #{pin_id}.", mention=False)
        return

    if subcmd != "add":
        bot.reply(
            msg,
            (
                f"Unknown subcommand '{subcmd}'. "
                f"Use {_prefix()}pin add|list|show|delete|on|off|status"
            ),
            mention=False,
        )
        return

    if not is_room:
        bot.reply(msg, f"ℹ️ To create a pin, use {_prefix()}pin add as a reply or {_prefix()}pin add last")
        return

    if not await _is_enabled_for_room(bot, room):
        bot.reply(msg, "ℹ️ Pin plugin is disabled in this room.")
        return

    # permission guard for manual add/add last
    if not await _sender_can_manage_pins_in_room(bot, msg, room):
        bot.reply(msg, "⛔ Only room moderators/admins/owners can add pins.", mention=False)
        return

    if len(args) >= 2 and str(args[1]).lower() == "last":
        offset = 1
        if len(args) >= 3:
            try:
                offset = int(args[2])
                if offset < 1:
                    raise ValueError
            except ValueError:
                bot.reply(msg, f"❌ Usage: {_prefix()}pin add last [n]")
                return

        recent_entry = _get_recent_target(room, offset=offset)
        if not recent_entry:
            if offset == 1:
                bot.reply(msg, f"❌ No suitable cached message found for {_prefix()}pin add last")
            else:
                bot.reply(msg, f"❌ No suitable cached message found for {_prefix()}pin add last {offset}")
            return

        await _create_pin_entry(
            bot=bot,
            msg=msg,
            room=room,
            sender_jid=sender_jid,
            nick=nick,
            target_text=recent_entry.get("body"),
            target_nick=recent_entry.get("nick") or "unknown",
            target_stanza_id=recent_entry.get("stanza_id"),
            reply_id=None,
            quote_text=None,
            cmd_body=msg.get("body", "") or "",
            source=f"last-{offset}",
        )
        return

    bot.reply(
        msg,
        f"❌ Reply to a room message and then send {_prefix()}pin add, or use {_prefix()}pin add last",
        mention=False,
    )


async def on_load(bot):
    bot.bot_plugins.register_event(
        "pin",
        "groupchat_message",
        partial(_on_groupchat_message, bot),
    )
