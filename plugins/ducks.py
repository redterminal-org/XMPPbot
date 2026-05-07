"""
Duck Game plugin for XMPP MUCs.

Randomly spawns ducks in enabled group chats. Users can befriend or trap them,
and room/user statistics are stored persistently.

This plugin only works in:
- public MUCs
- MUC private messages for on/off/status

It does not support normal 1:1 direct messages.

Commands:
    {prefix}duck on|off|status        - Enable/disable ducks in this room (MUC DM only)
    {prefix}duck befriend             - Befriend the current duck
    {prefix}duck trap                 - Trap the current duck
    {prefix}duck friends              - Show top duck friends
    {prefix}duck top                  - Alias for top duck friends
    {prefix}duck enemies              - Show top duck enemies
    {prefix}duck stats [jid|nickname] - Show duck stats

Aliases:
    {prefix}bef
    {prefix}trap
    {prefix}duckstats [jid|nickname]
"""

import asyncio
import logging
import random
import time
from collections import defaultdict
from datetime import date
from functools import partial

from slixmpp import JID

from utils.command import command, Role
from utils.config import config
from plugins._core import (
    _is_muc_pm,
    _is_enabled_for_room,
    handle_room_toggle_command,
    JOINED_ROOMS,
    _ensure_user_exists,
    _is_public_muc,
    )

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "ducks",
    "version": "1.3.0",
    "description": "Duck game for MUCs with room toggles and leaderboards",
    "category": "fun",
    "requires": ["rooms", "_core"],
}

DUCK = r"・゜゜・。。・゜゜\_o< QUACK!"

DUCKS_KEY = "DUCKS"
DUCKS_INDEX_KEY = "DUCKS_ROOM_INDEX"
DUCKS_LAST_KEY = "DUCKS_LAST"
DUCKS_DAILY_KEY = "DUCKS_DAILY"

duck_cfg = config.get("ducks", {})
DEFAULT_MIN_MESSAGES = duck_cfg.get("min_messages", 150)
DEFAULT_MAX_MESSAGES = duck_cfg.get("max_messages", 500)
DUCK_SPAWN_CHANCE = duck_cfg.get("spawn_chance", 20)
MAX_DUCKS_PER_DAY = duck_cfg.get("max_ducks_per_day", 3)
DUCK_TIMEOUT = duck_cfg.get("timeout", 0)
COUNT_COMMAND_MESSAGES = duck_cfg.get("count_commands", False)

ACTIVE_DUCKS = {}              # room_jid -> timestamp
PENDING_DUCKS = set()          # room_jid waiting for delayed spawn
MESSAGE_COUNTS = defaultdict(int)   # room_jid -> message counter, -1 means duck scheduled
NEXT_DUCK_THRESHOLDS = {}      # room_jid -> random threshold before spawn rolls begin
SPAWN_TASKS = {}               # room_jid -> asyncio.Task
EXPIRE_TASKS = {}              # room_jid -> asyncio.Task

BEFRIEND_REACTIONS = [
    "The duck waddles happily. 🦆💕",
    "The duck quacks with joy! 🦆",
    "The duck nuzzles you affectionately. 🦆✨",
    "The duck follows you around now. 🦆",
    "The duck seems to trust you. 🦆🤝",
    "The duck accepts your friendship. 🦆💛",
    "The duck does a tiny happy flap. 🦆",
    "The duck looks very pleased. 🦆😊",
    "The duck gifts you an invisible breadcrumb. 🦆🍞",
    "The duck has decided you are acceptable. 🦆",
]

TRAP_REACTIONS = [
    "The duck falls right into the trap! 🦆🪤",
    "Got it! The duck has been trapped. 🦆",
    "A perfect trap. The duck never stood a chance. 🦆🎯",
    "Snap! The duck is caught. 🦆",
    "The duck is contained. For now. 🦆📦",
    "Direct hit. The duck is trapped. 🦆💥",
    "The duck is outplayed. 🦆😵",
    "Successful trap deployment. 🦆🔒",
    "The duck walks straight into your evil plan. 🦆😈",
    "The duck underestimated you. Fatal mistake. 🦆",
]

NO_DUCK_REACTIONS = [
    "There was no duck!",
    "Too early. No duck in sight.",
    "You startled an imaginary duck.",
    "No duck here. Just vibes.",
    "The duck committee denies all allegations.",
    "You swing at empty air. No duck!",
    "A distant duck laughs at your attempt.",
    "False alarm. No duck detected.",
]

SPAWN_MESSAGES = [
    DUCK,
    f"{DUCK}\nA wild duck appears!",
    f"{DUCK}\nThe duck waddles into the room.",
    f"{DUCK}\nQUACK! A duck has appeared.",
    f"{DUCK}\nThe duck looks around cautiously.",
    f"{DUCK}\nA suspicious duck has entered the chat.",
]

DUCK_EXPIRE_MESSAGES = [
    "🦆 The duck waddles away.",
    "🦆 The duck loses interest and disappears.",
    "🦆 The duck escapes while nobody is looking.",
    "🦆 The duck flies off into the distance.",
]


async def get_ducks_store(bot):
    return bot.db.users.plugin("ducks")


def _duck_reply(bot, msg, text: str):
    bot.reply(
        msg,
        text,
        mention=False,
        thread=True,
    )


def _today_str() -> str:
    return date.today().isoformat()


def _get_random_threshold() -> int:
    min_msgs = int(DEFAULT_MIN_MESSAGES)
    max_msgs = int(DEFAULT_MAX_MESSAGES)
    if max_msgs < min_msgs:
        max_msgs = min_msgs
    return random.randint(min_msgs, max_msgs)


def _ensure_threshold(room_jid: str) -> int:
    threshold = NEXT_DUCK_THRESHOLDS.get(room_jid)
    if threshold is None:
        threshold = _get_random_threshold()
        NEXT_DUCK_THRESHOLDS[room_jid] = threshold
    return threshold


def _reset_room_cycle(room_jid: str):
    MESSAGE_COUNTS[room_jid] = 0
    NEXT_DUCK_THRESHOLDS[room_jid] = _get_random_threshold()


async def _get_daily_duck_data(bot):
    store = await get_ducks_store(bot)
    return await store.get_global(DUCKS_DAILY_KEY, default={})


async def _get_daily_duck_count(bot, room_jid: str) -> int:
    data = await _get_daily_duck_data(bot)
    room_data = data.get(room_jid, {})
    if room_data.get("date") != _today_str():
        return 0
    return int(room_data.get("count", 0))


async def _increment_daily_duck_count(bot, room_jid: str) -> int:
    store = await get_ducks_store(bot)
    data = await store.get_global(DUCKS_DAILY_KEY, default={})
    today = _today_str()

    room_data = data.get(room_jid, {})
    if room_data.get("date") != today:
        room_data = {"date": today, "count": 0}

    room_data["count"] = int(room_data.get("count", 0)) + 1
    data[room_jid] = room_data

    await store.set_global(DUCKS_DAILY_KEY, data)
    return room_data["count"]


def _normalize_bare_jid(value) -> str | None:
    if not value:
        return None
    try:
        return str(JID(str(value)).bare)
    except Exception:
        value = str(value)
        return value.split("/", 1)[0]


async def _resolve_real_jid(bot, msg) -> str | None:
    room = msg["from"].bare
    nick = msg.get("mucnick") or msg["from"].resource

    muc = bot.plugin.get("xep_0045", None)
    if muc:
        try:
            real_jid = muc.get_jid_property(room, nick, "jid")
            bare = _normalize_bare_jid(real_jid)
            if bare:
                return bare
        except Exception:
            pass

    try:
        cached = JOINED_ROOMS.get(room, {}).get("nicks", {}).get(nick, {})
        bare = _normalize_bare_jid(cached.get("jid"))
        if bare:
            return bare
    except Exception:
        pass

    return None


async def _get_last_duck_time(bot, room_jid):
    store = await get_ducks_store(bot)
    data = await store.get_global(DUCKS_LAST_KEY, default={})
    return data.get(room_jid)


async def _set_last_duck_time(bot, room_jid):
    store = await get_ducks_store(bot)
    data = await store.get_global(DUCKS_LAST_KEY, default={})
    data[room_jid] = time.time()
    await store.set_global(DUCKS_LAST_KEY, data)


async def _record_action(bot, room_jid, user_jid, display_name, action):
    store = await get_ducks_store(bot)

    await _ensure_user_exists(bot, user_jid, display_name)

    user_stats = await store.get(user_jid, "stats") or {
        "display_name": display_name,
        "befriended": 0,
        "trapped": 0,
        "rooms": {},
    }

    user_stats["display_name"] = display_name
    user_stats[action] = int(user_stats.get(action, 0)) + 1

    room_stats = user_stats["rooms"].setdefault(room_jid, {
        "befriended": 0,
        "trapped": 0,
    })
    room_stats[action] = int(room_stats.get(action, 0)) + 1

    await store.set(user_jid, "stats", user_stats)

    room_index = await store.get_global(DUCKS_INDEX_KEY, default={})
    room_entry = room_index.setdefault(room_jid, {})
    room_entry[user_jid] = {
        "display_name": display_name,
        "befriended": room_stats["befriended"],
        "trapped": room_stats["trapped"],
    }
    await store.set_global(DUCKS_INDEX_KEY, room_index)

    return user_stats[action], room_stats[action]


def _format_top(entries):
    if not entries:
        return "none yet"

    parts = []
    for i, entry in enumerate(entries, 1):
        parts.append(f"#{i} {entry['display_name']} ({entry['count']})")
    return " · ".join(parts)


async def _get_top(bot, stat_key, limit=10):
    store = await get_ducks_store(bot)
    room_index = await store.get_global(DUCKS_INDEX_KEY, default={})

    combined = {}

    for _, room_data in room_index.items():
        for user_jid, data in room_data.items():
            entry = combined.setdefault(user_jid, {
                "display_name": data.get("display_name", user_jid),
                "count": 0,
            })
            entry["display_name"] = data.get("display_name", entry["display_name"])
            entry["count"] += int(data.get(stat_key, 0))

    entries = list(combined.values())
    entries.sort(key=lambda item: (-item["count"], item["display_name"].lower()))
    return entries[:limit]


async def _get_user_stats(bot, target: str):
    store = await get_ducks_store(bot)

    exact = await store.get(target, "stats")
    if exact:
        return exact

    target_lower = target.lower()
    room_index = await store.get_global(DUCKS_INDEX_KEY, default={})

    totals = {
        "display_name": None,
        "befriended": 0,
        "trapped": 0,
        "rooms": {},
    }
    found = False

    for room_jid, room_data in room_index.items():
        for _, data in room_data.items():
            display_name = data.get("display_name", "")
            if display_name.lower() != target_lower:
                continue

            found = True
            if not totals["display_name"]:
                totals["display_name"] = display_name

            bef = int(data.get("befriended", 0))
            trap = int(data.get("trapped", 0))
            totals["befriended"] += bef
            totals["trapped"] += trap
            totals["rooms"][room_jid] = {
                "befriended": bef,
                "trapped": trap,
            }

    return totals if found else None


async def _expire_duck(bot, room_jid):
    try:
        await asyncio.sleep(DUCK_TIMEOUT)

        if room_jid not in ACTIVE_DUCKS:
            return

        ACTIVE_DUCKS.pop(room_jid, None)
        await _set_last_duck_time(bot, room_jid)

        bot.reply(
            {
                "from": type("F", (), {"bare": room_jid})(),
                "type": "groupchat",
            },
            random.choice(DUCK_EXPIRE_MESSAGES),
            mention=False,
            thread=True,
            rate_limit=False,
            ephemeral=False,
        )
        log.info("[DUCKS] Duck expired in %s", room_jid)

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[DUCKS] Failed to expire duck in %s", room_jid)
    finally:
        EXPIRE_TASKS.pop(room_jid, None)


async def _spawn_duck_after_delay(bot, room_jid, delay):
    try:
        await asyncio.sleep(delay)

        if room_jid not in PENDING_DUCKS:
            return

        daily_count = await _get_daily_duck_count(bot, room_jid)
        if MAX_DUCKS_PER_DAY > 0 and daily_count >= MAX_DUCKS_PER_DAY:
            log.info("[DUCKS] Daily duck limit reached in %s", room_jid)
            _reset_room_cycle(room_jid)
            return

        PENDING_DUCKS.discard(room_jid)
        ACTIVE_DUCKS[room_jid] = time.time()
        _reset_room_cycle(room_jid)

        await _increment_daily_duck_count(bot, room_jid)

        old_expire = EXPIRE_TASKS.pop(room_jid, None)
        if old_expire:
            old_expire.cancel()

        if DUCK_TIMEOUT > 0:
            EXPIRE_TASKS[room_jid] = asyncio.create_task(_expire_duck(bot, room_jid))

        bot.reply(
            {
                "from": type("F", (), {"bare": room_jid})(),
                "type": "groupchat",
            },
            random.choice(SPAWN_MESSAGES),
            mention=False,
            thread=True,
            rate_limit=False,
            ephemeral=False,
        )
        log.info("[DUCKS] Duck spawned in %s", room_jid)

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[DUCKS] Failed to spawn duck in %s", room_jid)
    finally:
        PENDING_DUCKS.discard(room_jid)
        SPAWN_TASKS.pop(room_jid, None)


async def _maybe_schedule_duck(bot, room_jid):
    if room_jid in ACTIVE_DUCKS or room_jid in PENDING_DUCKS:
        return

    daily_count = await _get_daily_duck_count(bot, room_jid)
    if MAX_DUCKS_PER_DAY > 0 and daily_count >= MAX_DUCKS_PER_DAY:
        return

    if MESSAGE_COUNTS[room_jid] == -1:
        return

    MESSAGE_COUNTS[room_jid] += 1

    threshold = _ensure_threshold(room_jid)
    if MESSAGE_COUNTS[room_jid] < threshold:
        return

    if random.randint(1, DUCK_SPAWN_CHANCE) != 1:
        return

    MESSAGE_COUNTS[room_jid] = -1
    delay = random.randint(5, 20)
    PENDING_DUCKS.add(room_jid)
    SPAWN_TASKS[room_jid] = asyncio.create_task(
        _spawn_duck_after_delay(bot, room_jid, delay)
    )
    log.info(
        "[DUCKS] Duck scheduled for %s in %ss (threshold=%s)",
        room_jid,
        delay,
        threshold,
    )


async def _handle_no_duck(bot, msg, room_jid, display_name):
    message = random.choice(NO_DUCK_REACTIONS)

    last_duck = await _get_last_duck_time(bot, room_jid)
    if last_duck is not None:
        seconds = round(time.time() - last_duck, 2)
        message += f" Missed by {seconds} seconds."

    _duck_reply(bot, msg, f"❌ {display_name}: {message}")


async def _handle_duck_action(bot, msg, action):
    room_jid = msg["from"].bare
    display_name = msg.get("mucnick") or msg["from"].resource or "Unknown"

    user_jid = await _resolve_real_jid(bot, msg)
    if not user_jid:
        _duck_reply(bot, msg, "❌ Could not determine your JID in this room.")
        return

    if room_jid not in ACTIVE_DUCKS:
        await _handle_no_duck(bot, msg, room_jid, display_name)
        return

    duck_timestamp = ACTIVE_DUCKS.pop(room_jid)
    await _set_last_duck_time(bot, room_jid)

    expire_task = EXPIRE_TASKS.pop(room_jid, None)
    if expire_task:
        expire_task.cancel()

    seconds = round(time.time() - duck_timestamp, 2)

    if action == "befriended":
        verb = "befriended"
        reaction = random.choice(BEFRIEND_REACTIONS)
    else:
        verb = "trapped"
        reaction = random.choice(TRAP_REACTIONS)

    overall_count, room_count = await _record_action(
        bot, room_jid, user_jid, display_name, action
    )

    plural = "duck" if overall_count == 1 else "ducks"

    _duck_reply(
        bot,
        msg,
        (
            f"✅ {display_name} {verb} a duck in {seconds} seconds!\n"
            f"{reaction}\n"
            f"You've {verb} {overall_count} {plural} overall "
            f"({room_count} in {room_jid})."
        ),
    )

    log.info("[DUCKS] %s (%s) %s a duck in %s", display_name, user_jid, verb, room_jid)


@command("duck", role=Role.USER)
async def duck_command(bot, sender_jid, nick, args, msg, is_room):
    handled = await handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_ducks_store,
        key=DUCKS_KEY,
        label="Duck game",
        storage="dict",
        log_prefix="[DUCKS]",
    )
    if handled:
        room_jid = msg["from"].bare
        if _is_muc_pm(msg) and args and args[0].lower() == "off":
            ACTIVE_DUCKS.pop(room_jid, None)
            PENDING_DUCKS.discard(room_jid)
            _reset_room_cycle(room_jid)

            spawn_task = SPAWN_TASKS.pop(room_jid, None)
            if spawn_task:
                spawn_task.cancel()

            expire_task = EXPIRE_TASKS.pop(room_jid, None)
            if expire_task:
                expire_task.cancel()
        return

    is_muc_pm = _is_muc_pm(msg)
    is_public_room = _is_public_muc(msg, is_room)

    if not is_public_room:
        if is_muc_pm and args:
            _duck_reply(
                bot,
                msg,
                "ℹ️ Use 'duck on/off/status' here, but duck gameplay only works in the public room.",
            )
        return

    room_jid = msg["from"].bare
    if not await _is_enabled_for_room(bot, DUCKS_KEY, "ducks", room_jid):
        return

    if not args:
        _duck_reply(
            bot,
            msg,
            (
                f"🦆 Usage: {config.get('prefix', ',')}duck "
                "befriend|trap|friends|top|enemies|stats [jid|nickname]"
            ),
        )
        return

    sub = args[0].lower()

    if sub == "befriend":
        await _handle_duck_action(bot, msg, "befriended")
        return

    if sub == "trap":
        await _handle_duck_action(bot, msg, "trapped")
        return

    if sub in ("friends", "top"):
        top = await _get_top(bot, "befriended", limit=10)
        _duck_reply(bot, msg, f"👥 Top duck friends: {_format_top(top)}")
        return

    if sub == "enemies":
        top = await _get_top(bot, "trapped", limit=10)
        _duck_reply(bot, msg, f"🎯 Top duck enemies: {_format_top(top)}")
        return

    if sub == "stats":
        target = " ".join(args[1:]).strip() if len(args) > 1 else await _resolve_real_jid(bot, msg)
        if not target:
            _duck_reply(bot, msg, "❌ Could not determine target user.")
            return

        stats = await _get_user_stats(bot, target)
        if not stats:
            _duck_reply(bot, msg, "📊 No duck stats found for that user.")
            return

        room_stats = stats.get("rooms", {}).get(room_jid, {})
        current_bef = int(room_stats.get("befriended", 0))
        current_trap = int(room_stats.get("trapped", 0))
        safe_name = stats.get("display_name") or "That user"

        _duck_reply(
            bot,
            msg,
            (
                f"📊 {safe_name} has befriended "
                f"{int(stats.get('befriended', 0))} and trapped "
                f"{int(stats.get('trapped', 0))} ducks "
                f"({current_bef}/{current_trap} in {room_jid})"
            ),
        )
        return

    _duck_reply(bot, msg, "❌ Unknown duck subcommand.")


@command("bef", role=Role.USER)
async def bef_command(bot, sender_jid, nick, args, msg, is_room):
    if not _is_public_muc(msg, is_room):
        return
    if not await _is_enabled_for_room(bot, DUCKS_KEY, "ducks", msg["from"].bare):
        return
    await _handle_duck_action(bot, msg, "befriended")


@command("trap", role=Role.USER)
async def trap_command(bot, sender_jid, nick, args, msg, is_room):
    if not _is_public_muc(msg, is_room):
        return
    if not await _is_enabled_for_room(bot, DUCKS_KEY, "ducks", msg["from"].bare):
        return
    await _handle_duck_action(bot, msg, "trapped")


@command("duckstats", role=Role.USER)
async def duckstats_command(bot, sender_jid, nick, args, msg, is_room):
    if not _is_public_muc(msg, is_room):
        return
    if not await _is_enabled_for_room(bot, DUCKS_KEY, "ducks", msg["from"].bare):
        return
    await duck_command(bot, sender_jid, nick, ["stats", *args], msg, is_room)


async def on_message(bot, msg):
    try:
        body = msg.get("body", "").strip()
        if not body:
            return

        if msg.get("from") == bot.boundjid:
            return

        if msg.get("type") != "groupchat":
            return

        if not COUNT_COMMAND_MESSAGES:
            prefix = config.get("prefix", ",")
            if body.startswith(prefix):
                return

        room_jid = msg["from"].bare

        if not await _is_enabled_for_room(bot, DUCKS_KEY, "ducks", room_jid):
            return

        bot_nick = bot.presence.joined_rooms.get(room_jid)
        if bot_nick and bot_nick == msg.get("mucnick"):
            return

        await _maybe_schedule_duck(bot, room_jid)

    except Exception:
        log.exception("[DUCKS] Error in on_message")


async def on_load(bot):
    log.info("[DUCKS] Plugin loading...")

    bot.bot_plugins.register_event(
        "ducks",
        "groupchat_message",
        partial(on_message, bot),
    )

    log.info("[DUCKS] Plugin loaded")


async def on_unload(bot):
    for task in list(SPAWN_TASKS.values()):
        task.cancel()
    for task in list(EXPIRE_TASKS.values()):
        task.cancel()

    SPAWN_TASKS.clear()
    EXPIRE_TASKS.clear()
    ACTIVE_DUCKS.clear()
    PENDING_DUCKS.clear()
    MESSAGE_COUNTS.clear()
    NEXT_DUCK_THRESHOLDS.clear()

    log.info("[DUCKS] Plugin unloaded")
