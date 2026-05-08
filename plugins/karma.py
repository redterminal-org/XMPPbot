"""
Room-local karma plugin with nick++ / nick-- tracking.

Provides simple room-local karma tracking using patterns like:

    nick++
    nick--

Control:
    {prefix}karma on|off|status   - Enable/disable karma in this room
                                    (MUC DM only)

Queries:
    {prefix}karma <nick>          - Show karma for a nick in this room
    {prefix}karma top             - Show top karma in this room
    {prefix}karma bottom          - Show lowest karma in this room
"""

import logging
import re
import time
from functools import partial

from slixmpp import JID

from utils.command import command, Role
from plugins._core import (
    _is_muc_pm,
    JOINED_ROOMS,
    handle_room_toggle_command,
    _is_enabled_for_room,
    _is_public_muc,
)
from plugins.sed import is_sed_command

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "karma",
    "version": "1.2.0",
    "description": "Room-local karma tracking with nick++ / nick--",
    "category": "fun",
    "requires": ["rooms", "_core"],
}

KARMA_ENABLED_KEY = "KARMA"
KARMA_SCORES_KEY = "scores"

KARMA_DELAY_SECONDS = 60
LAST_KARMA_ACTIONS = {}  # room:actor -> {target_lower: timestamp}

OP_RE = re.compile(r"(\+\+|--)")


async def get_karma_store(bot):
    return bot.db.users.plugin("karma")


def _karma_reply(bot, msg, text: str):
    bot.reply(msg, text, mention=False, thread=True)


async def _get_room_scores(bot, room_jid: str) -> dict:
    store = await get_karma_store(bot)
    data = await store.get_global(KARMA_SCORES_KEY, default={})

    if not isinstance(data, dict):
        data = {}

    room_scores = data.get(room_jid, {})
    if not isinstance(room_scores, dict):
        room_scores = {}

    normalized = {}
    for nick, value in room_scores.items():
        try:
            normalized[str(nick)] = int(value)
        except Exception:
            normalized[str(nick)] = 0

    return normalized


async def _set_room_scores(bot, room_jid: str, scores: dict):
    store = await get_karma_store(bot)
    data = await store.get_global(KARMA_SCORES_KEY, default={})

    if not isinstance(data, dict):
        data = {}

    data[room_jid] = scores
    await store.set_global(KARMA_SCORES_KEY, data)


async def _resolve_real_jid(bot, msg) -> str | None:
    room = msg["from"].bare
    nick = msg.get("mucnick") or msg["from"].resource

    muc = bot.plugin.get("xep_0045", None)
    if muc:
        try:
            real_jid = muc.get_jid_property(room, nick, "jid")
            if real_jid:
                return str(JID(str(real_jid)).bare)
        except Exception:
            pass

    try:
        cached = JOINED_ROOMS.get(room, {}).get("nicks", {}).get(nick, {})
        jid = cached.get("jid")
        if jid:
            return str(JID(str(jid)).bare)
    except Exception:
        pass

    return None


def _get_bot_nick(room_jid: str) -> str | None:
    room = JOINED_ROOMS.get(room_jid, {})
    nick = room.get("nick")
    return str(nick) if nick else None


def _known_room_nicks(room_jid: str) -> list[str]:
    nicks = JOINED_ROOMS.get(room_jid, {}).get("nicks", {})
    result = [str(nick) for nick in nicks.keys() if str(nick).strip()]
    result.sort(key=lambda n: (-len(n), n.lower()))
    return result


def _canonical_nick(room_jid: str, nick: str) -> str:
    target_lower = nick.strip().lower()

    for known_nick in _known_room_nicks(room_jid):
        if known_nick.lower() == target_lower:
            return known_nick

    return nick.strip()


def _normalize_lookup(scores: dict, target: str):
    target_lower = target.lower()

    for nick, score in scores.items():
        if str(nick).lower() == target_lower:
            return nick, int(score)

    return None, 0


def _format_entry(idx: int, nick: str, score: int) -> str:
    return f"#{idx} {nick} ({score})"


def _format_ranking(entries: list[tuple[str, int]]) -> str:
    if not entries:
        return "none yet"

    return " · ".join(
        _format_entry(i, nick, score)
        for i, (nick, score) in enumerate(entries, 1)
    )


def _left_boundary_ok(text: str, start: int) -> bool:
    if start <= 0:
        return True
    prev = text[start - 1]
    return prev.isspace() or prev in "([{'\"“”„‚<>|/,:;*"


def _match_known_nick_before_operator(text: str, op_start: int, room_jid: str) -> str | None:
    prefix = text[:op_start].rstrip()
    if not prefix:
        return None

    for known_nick in _known_room_nicks(room_jid):
        if len(prefix) < len(known_nick):
            continue

        candidate = prefix[-len(known_nick):]
        if candidate.lower() != known_nick.lower():
            continue

        start = len(prefix) - len(known_nick)
        if not _left_boundary_ok(prefix, start):
            continue

        return known_nick

    return None


def _extract_karma_events(body: str, room_jid: str) -> list[tuple[str, int]]:
    events = []

    for match in OP_RE.finditer(body):
        op = match.group(1)
        op_start = match.start(1)

        nick = _match_known_nick_before_operator(body, op_start, room_jid)
        if not nick:
            continue

        delta = 1 if op == "++" else -1
        events.append((nick, delta))

    return events


async def _actor_throttle_key(bot, msg) -> str:
    room_jid = msg["from"].bare
    real_jid = await _resolve_real_jid(bot, msg)

    if real_jid:
        return f"{room_jid}:{real_jid.lower()}"

    actor_nick = msg.get("mucnick") or msg["from"].resource or "unknown"
    return f"{room_jid}:nick:{actor_nick.lower()}"


async def _check_throttle(bot, msg, target_nick: str) -> bool:
    key = await _actor_throttle_key(bot, msg)
    entry = LAST_KARMA_ACTIONS.get(key, {})
    ts = entry.get(target_nick.lower())
    return ts is None or (time.time() - ts) >= KARMA_DELAY_SECONDS


async def _set_throttle(bot, msg, target_nick: str):
    key = await _actor_throttle_key(bot, msg)
    entry = LAST_KARMA_ACTIONS.setdefault(key, {})
    entry[target_nick.lower()] = time.time()


@command("karma", role=Role.USER)
async def karma_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Karma control and queries.

    Usage:
        {prefix}karma on
        {prefix}karma off
        {prefix}karma status
        {prefix}karma <nick>
        {prefix}karma top
        {prefix}karma bottom
    """
    handled = await handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_karma_store,
        key=KARMA_ENABLED_KEY,
        label="Karma",
        storage="dict",
        log_prefix="[KARMA]",
    )
    if handled:
        return

    is_muc_pm = _is_muc_pm(msg)
    is_public_room = _is_public_muc(msg, is_room)

    if not is_public_room:
        if is_muc_pm:
            _karma_reply(
                bot,
                msg,
                "ℹ️ Use 'karma on/off/status' here. Karma queries work in the public room.",
            )
        return

    room_jid = msg["from"].bare
    if not await _is_enabled_for_room(bot, KARMA_ENABLED_KEY, "karma", room_jid):
        return

    if not args:
        _karma_reply(
            bot,
            msg,
            f"Usage: {bot.prefix}karma <on|off|status|top|bottom|nick>",
        )
        return

    sub = " ".join(args).strip()

    if len(args) == 1 and args[0].lower() == "top":
        scores = await _get_room_scores(bot, room_jid)
        entries = sorted(
            scores.items(),
            key=lambda item: (-int(item[1]), item[0].lower())
        )[:10]
        _karma_reply(bot, msg, f"🏆 Karma top for this room: {_format_ranking(entries)}")
        return

    if len(args) == 1 and args[0].lower() == "bottom":
        scores = await _get_room_scores(bot, room_jid)
        entries = sorted(
            scores.items(),
            key=lambda item: (int(item[1]), item[0].lower())
        )[:10]
        _karma_reply(bot, msg, f"💀 Karma bottom for this room: {_format_ranking(entries)}")
        return

    target = _canonical_nick(room_jid, sub)
    known_targets = {n.lower() for n in _known_room_nicks(room_jid)}
    if target.lower() not in known_targets:
        _karma_reply(bot, msg, f"❌ '{sub}' is not currently in this room.")
        return

    scores = await _get_room_scores(bot, room_jid)
    canonical, score = _normalize_lookup(scores, target)
    display = canonical or target

    _karma_reply(bot, msg, f"📊 Karma for {display}: {score}")


async def on_message(bot, msg):
    try:
        body = msg.get("body", "").strip()
        if not body:
            return

        if is_sed_command(body):
            return

        if msg.get("type") != "groupchat":
            return

        room_jid = msg["from"].bare
        if room_jid not in JOINED_ROOMS:
            return

        actor_nick = msg.get("mucnick") or msg["from"].resource
        if not actor_nick:
            return

        bot_nick = _get_bot_nick(room_jid)
        if bot_nick and actor_nick.lower() == bot_nick.lower():
            return

        if not await _is_enabled_for_room(bot, KARMA_ENABLED_KEY,
                                          "karma", room_jid):
            return

        events = _extract_karma_events(body, room_jid)
        if not events:
            return

        scores = await _get_room_scores(bot, room_jid)
        changed = False
        seen_targets = set()
        response_lines = []
        throttle_hit = False

        for raw_target, delta in events:
            target_nick = _canonical_nick(room_jid, raw_target)
            target_lower = target_nick.lower()

            if target_lower in seen_targets:
                continue
            seen_targets.add(target_lower)

            if target_lower == str(actor_nick).lower():
                continue

            allowed = await _check_throttle(bot, msg, target_nick)
            if not allowed:
                throttle_hit = True
                continue

            current_key, current_score = _normalize_lookup(scores, target_nick)
            key = current_key or target_nick
            scores[key] = int(current_score) + delta
            changed = True

            await _set_throttle(bot, msg, key)

            sign = "+1" if delta > 0 else "-1"
            icon = "📈" if delta > 0 else "📉"
            response_lines.append(
                f"{icon} {key} now has {scores[key]} karma ({sign} from {actor_nick})"
            )

            log.info(
                "[KARMA] room=%s actor=%s target=%s delta=%s total=%s",
                room_jid,
                actor_nick,
                key,
                delta,
                scores[key],
            )

        if changed:
            await _set_room_scores(bot, room_jid, scores)

        if response_lines:
            _karma_reply(bot, msg, "\n".join(response_lines))

        if throttle_hit and not response_lines:
            _karma_reply(bot, msg, "⏱️ You recently gave karma to that user. Try again later.")

    except Exception:
        log.exception("[KARMA] Error in on_message")


async def on_load(bot):
    log.info("[KARMA] Plugin loading...")
    bot.bot_plugins.register_event(
        "karma",
        "groupchat_message",
        partial(on_message, bot),
    )
    log.info("[KARMA] Plugin loaded")


async def on_unload(bot):
    LAST_KARMA_ACTIONS.clear()
    log.info("[KARMA] Plugin unloaded")
