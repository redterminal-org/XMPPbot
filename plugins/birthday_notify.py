"""Birthday notification plugin.

Automatically sends birthday greetings in rooms when:
- Birthday notifications are ENABLED for the room via MUC direct message
- It's the user's birthday based on the vCard BDAY field
- The user is currently present in the room
- The notification hasn't been sent yet today in that room

Features:
- Per-room opt-in
- Non-blocking startup: birthday scans run in the background
- Instant notification when user joins room on their birthday
- Multi-room support
- Tracks sent notifications per room/user/day
- Caches positive and negative BDAY lookup results in this plugin's DB store
- Does not modify or depend on vCard plugin internals
- Handles MM-DD, YYYY-MM-DD, MMDD, YYYYMMDD and --MM-DD birthday formats

Commands:
• {prefix}birthday_notify on
• {prefix}birthday_notify off
• {prefix}birthday_notify status
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from functools import partial
from typing import Any

from utils.command import command, Role
from utils.config import config
from plugins._core import (
    handle_room_toggle_command,
    JOINED_ROOMS,
    _ensure_user_exists,
    _is_enabled_for_room,
)
# --------------------------------------------------------------------------
# !!!Switched from plugins._core.get_profile to plugins.vcard.get_user_vcard
# due to circular import!!!
# --------------------------------------------------------------------------
from plugins.vcard import get_user_vcard as get_profile

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "birthday_notify",
    "version": "1.1.1",
    "description":
        "Automatic birthday notifications in rooms (opt-in per room)",
    "category": "fun",
    "requires": ["rooms", "_core"],
}

# Track announcements in memory: {(room_jid, user_jid): "YYYY-MM-DD"}
ANNOUNCED_TODAY: dict[tuple[str, str], str] = {}

# Background task for periodic birthday checks
_BIRTHDAY_CHECK_TASK: asyncio.Task | None = None

# How long cached BDAY lookup results are trusted before refreshing from live
# vCard. This caches both positive results and explicit "no BDAY found"#
# results.
BDAY_CACHE_TTL_SECONDS = 12 * 60 * 60

# Delay the first full scan so plugin startup does not block the bot.
INITIAL_SCAN_DELAY_SECONDS = 10

# Check whether a new day has started. The full room scan only runs once per
# day.
CHECK_LOOP_INTERVAL_SECONDS = 60 * 60


def _today() -> datetime.date:
    return datetime.date.today()


def _now_ts() -> int:
    return int(datetime.datetime.now(datetime.UTC).timestamp())


def _parse_birthday(birthday_str: str) -> dict[str, int | None] | None:
    """Parse vCard BDAY into components.

    Supported:
    - YYYY-MM-DD
    - YYYYMMDD
    - MM-DD
    - MMDD
    - --MM-DD

    Returns:
        {"month": int, "day": int, "year": int | None}
        or None if invalid.
    """
    if not birthday_str:
        return None

    value = str(birthday_str).strip()

    patterns = (
        # YYYY-MM-DD
        (r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$", True),
        # YYYYMMDD
        (r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})$", True),
        # MM-DD
        (r"^(?P<month>\d{2})-(?P<day>\d{2})$", False),
        # MMDD
        (r"^(?P<month>\d{2})(?P<day>\d{2})$", False),
        # --MM-DD
        (r"^--(?P<month>\d{2})-(?P<day>\d{2})$", False),
    )

    for pattern, has_year in patterns:
        match = re.match(pattern, value)
        if not match:
            continue

        year = int(match.group("year")) if has_year else None
        month = int(match.group("month"))
        day = int(match.group("day"))

        # Validate real calendar dates.
        # Use leap year 2000 for birthdays without a year so 02-29 is valid.
        validation_year = year or 2000

        try:
            datetime.date(validation_year, month, day)
        except ValueError:
            return None

        return {
            "month": month,
            "day": day,
            "year": year,
        }

    return None


def _is_birthday_today(birthday_str: str) -> bool:
    birthday_data = _parse_birthday(birthday_str)
    if not birthday_data:
        return False

    today = _today()
    return (today.month, today.day) == (
        birthday_data["month"],
        birthday_data["day"],
    )


def _calculate_age(birthday_str: str) -> int | None:
    birthday_data = _parse_birthday(birthday_str)
    if not birthday_data or not birthday_data.get("year"):
        return None

    today = _today()
    age = today.year - birthday_data["year"]

    if (today.month, today.day) < (
        birthday_data["month"],
        birthday_data["day"],
    ):
        age -= 1

    return age


def _normalize_bday_value(value: Any) -> str | None:
    """Normalize BDAY values returned by get_profile() dict."""
    if value is None:
        return None

    # Some helpers/plugins use string placeholders.
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "none",
        "null",
        "—",
        "-",
    }:
        return None

    # Be defensive in case a future vCard implementation returns a list.
    if isinstance(value, list):
        for item in value:
            normalized = _normalize_bday_value(item)
            if normalized:
                return normalized
        return None

    return str(value).strip()


async def _store_get(store, jid: str, key: str, default=None):
    """Compatibility wrapper for stores with or without default= support."""
    try:
        return await store.get(jid, key, default=default)
    except TypeError:
        value = await store.get(jid, key)
        return default if value is None else value


async def _load_announced_date(bot, room_jid: str,
                               user_jid: str) -> str | None:
    """Load persisted announcement date for this room/user pair."""
    store = bot.db.users.plugin("birthday_notify")
    room_jid = str(room_jid)
    user_jid = str(user_jid)
    cache_key = (room_jid, user_jid)

    if cache_key in ANNOUNCED_TODAY:
        return ANNOUNCED_TODAY[cache_key]

    announced_by_room = await _store_get(
        store,
        user_jid,
        "announced_dates_by_room",
        default={},
    )

    if not isinstance(announced_by_room, dict):
        announced_by_room = {}

    announced_date = announced_by_room.get(room_jid)

    if announced_date:
        ANNOUNCED_TODAY[cache_key] = announced_date

    return announced_date


async def _mark_announced(bot, room_jid: str, user_jid: str, date_str: str):
    """Persist announcement date for this room/user pair."""
    await _ensure_user_exists(bot, user_jid)

    store = bot.db.users.plugin("birthday_notify")
    room_jid = str(room_jid)
    user_jid = str(user_jid)
    cache_key = (room_jid, user_jid)

    announced_by_room = await _store_get(
        store,
        user_jid,
        "announced_dates_by_room",
        default={},
    )

    if not isinstance(announced_by_room, dict):
        announced_by_room = {}

    announced_by_room[room_jid] = date_str
    ANNOUNCED_TODAY[cache_key] = date_str

    await store.set(user_jid, "announced_dates_by_room", announced_by_room)

    # Optional legacy key for visibility/backwards compatibility.
    await store.set(user_jid, "announced_date", date_str)

    await bot.db.users.flush_all()


async def _get_cached_bday(bot, user_jid: str):
    """Return cached BDAY state and updated_at timestamp for a user.

    Returns:
    - (birthday_string, updated_at) when a BDAY exists
    - (None, updated_at) when we recently checked and no BDAY exists
    - (None, None) when no usable cache exists
    """
    store = bot.db.users.plugin("birthday_notify")
    cache = await _store_get(
        store,
        str(user_jid),
        "cached_bday",
        default=None,
    )

    if not isinstance(cache, dict):
        return None, None

    updated_at = cache.get("updated_at")

    if not isinstance(updated_at, int):
        return None, None

    # Negative cache: we checked recently and found no BDAY.
    if cache.get("has_bday") is False:
        return None, updated_at

    value = _normalize_bday_value(cache.get("value"))

    if not value:
        return None, updated_at

    return value, updated_at


async def _set_cached_bday(bot, user_jid: str, birthday,
                           nick: str | None = None):
    """Store BDAY lookup result in this plugin's DB cache.

    This stores positive results and explicit negative results. It should
    not be called for transport errors where we do not know whether the
    user has a BDAY.
    """
    await _ensure_user_exists(bot, user_jid, nickname=nick)

    store = bot.db.users.plugin("birthday_notify")
    birthday = _normalize_bday_value(birthday)

    payload = {
        "has_bday": bool(birthday),
        "value": birthday,
        "updated_at": _now_ts(),
    }

    await store.set(str(user_jid), "cached_bday", payload)
    await bot.db.users.flush_all()


async def _get_birthday_from_vcard(bot, room_jid, nick: str):
    """Fetch BDAY from the vCard plugin using MUC nick context.

    Returns:
        (True, birthday_or_none) when the vCard lookup completed.
        (False, None) when the lookup failed/errored.

    This intentionally uses the public get_profile() helper dict and
    does not depend on vCard plugin internals.
    """
    try:
        lookup_msg = bot.make_message(
            mfrom=room_jid,
            mto=bot.boundjid.bare,
            mtype="chat",
            mbody="",
        )

        profile = await get_profile(bot, lookup_msg, f"{room_jid}/{nick}")
        birthday = profile.get("BDAY")
        return True, _normalize_bday_value(birthday)

    except Exception as exc:
        log.debug(
            "[BIRTHDAY] Couldn't fetch BDAY from vCard for %s in room %s: %s",
            nick,
            room_jid,
            exc,
        )
        return False, None


async def _get_birthday_cached_or_live(bot, room_jid,
                                       user_jid: str, nick: str):
    """Get BDAY from birthday_notify cache first, then live vCard if needed.

    Behavior:
    - fresh positive cache: use cached BDAY
    - fresh negative cache: skip live lookup and return None
    - missing/stale cache: refresh via get_profile() dict["BDAY"]
    - failed live lookup: fall back to stale positive cache if available
    - failed live lookup: do NOT create a negative cache entry
    """
    user_jid = str(user_jid)

    cached_bday, updated_at = await _get_cached_bday(bot, user_jid)

    if updated_at:
        cache_age = _now_ts() - updated_at

        if cache_age <= BDAY_CACHE_TTL_SECONDS:
            if cached_bday:
                log.debug(
                    "[BIRTHDAY] Using cached BDAY for %s, age=%ss",
                    user_jid,
                    cache_age,
                )
            else:
                log.debug(
                    "[BIRTHDAY] Using cached empty BDAY for %s, age=%ss",
                    user_jid,
                    cache_age,
                )

            return cached_bday

    lookup_ok, live_bday = await _get_birthday_from_vcard(bot, room_jid, nick)

    if lookup_ok:
        # Cache both positive and explicit negative lookup results.
        await _set_cached_bday(bot, user_jid, live_bday, nick=nick)
        return live_bday

    if cached_bday:
        log.debug(
            "[BIRTHDAY] Using stale cached BDAY for %s because live lookup"
            " failed",
            user_jid,
        )

    return cached_bday


async def _check_user_birthday(bot, user_jid_str: str, nick: str, room_jid):
    """Check if a specific user has birthday today and announce if needed."""
    try:
        room_jid_str = str(room_jid)
        user_jid_str = str(user_jid_str)
        today_str = _today().isoformat()

        announced_date = await _load_announced_date(
            bot,
            room_jid_str,
            user_jid_str,
        )

        if announced_date == today_str:
            return

        birthday = await _get_birthday_cached_or_live(
            bot,
            room_jid,
            user_jid_str,
            nick,
        )

        log.debug(
            "[BIRTHDAY] Checking %s (%s) in room %s - birthday: %s",
            nick,
            user_jid_str,
            room_jid_str,
            birthday,
        )

        if not birthday:
            return

        if not _is_birthday_today(birthday):
            return

        age = _calculate_age(birthday)

        if age is not None:
            msg_text = f"🎉 Happy Birthday {
                nick}! You're turning {age} today! 🎂"
        else:
            msg_text = f"🎉 Happy Birthday {nick}! 🎂"

        try:
            msg = bot.make_message(
                mto=room_jid,
                mbody=msg_text,
                mtype="groupchat",
            )
            await bot._safe_send_message(msg)
        except Exception as exc:
            log.exception("[BIRTHDAY] Failed to send birthday message: %s",
                          exc)
            return

        await _mark_announced(bot, room_jid_str, user_jid_str, today_str)

        log.info(
            "[BIRTHDAY] Birthday announcement for %s (%s) in room %s%s",
            nick,
            user_jid_str,
            room_jid_str,
            f" (age {age})" if age is not None else "",
        )

    except Exception as exc:
        log.exception("[BIRTHDAY] Error checking user birthday: %s", exc)


async def _check_room_birthdays(bot, room_jid: str):
    """Check all currently present users in one enabled room."""
    try:
        room_jid = str(room_jid)

        enabled = await _is_enabled_for_room(bot, "birthday_notify",
                                             "birthday_notify", room_jid)
        if not enabled:
            return

        room_data = JOINED_ROOMS.get(room_jid)
        if not isinstance(room_data, dict):
            return

        nicks_data = room_data.get("nicks", {})
        if not isinstance(nicks_data, dict):
            return

        for nick, nick_info in nicks_data.items():
            if not isinstance(nick_info, dict):
                continue

            user_jid = nick_info.get("jid")
            if not user_jid:
                continue

            await _check_user_birthday(
                bot,
                str(user_jid),
                nick,
                room_jid,
            )

            # Yield to the event loop during larger rooms.
            await asyncio.sleep(0)

    except Exception as exc:
        log.exception(
            "[BIRTHDAY] Error checking room birthdays for %s: %s",
            room_jid,
            exc,
        )


async def _check_and_announce_birthdays(bot):
    """Check all users for birthdays and announce in enabled rooms."""
    try:
        log.info("[BIRTHDAY] Already announced: %s", ANNOUNCED_TODAY)

        for room_jid in JOINED_ROOMS:
            await _check_room_birthdays(bot, str(room_jid))

    except Exception as exc:
        log.exception("[BIRTHDAY] Error in birthday check: %s", exc)


async def _birthday_check_loop(
    bot,
    check_interval: int = CHECK_LOOP_INTERVAL_SECONDS
):
    """Periodic task that checks for birthdays.

    The first full scan is delayed so the bot can finish startup quickly. After
    that, a full scan is performed once per calendar day. Join events still
    check users immediately, so late joiners are covered.
    """
    last_full_check_date: str | None = None

    try:
        await asyncio.sleep(INITIAL_SCAN_DELAY_SECONDS)

        while True:
            today_str = _today().isoformat()

            if last_full_check_date != today_str:
                await _check_and_announce_birthdays(bot)
                last_full_check_date = today_str

            await asyncio.sleep(check_interval)

    except asyncio.CancelledError:
        log.debug("[BIRTHDAY] ✅ Birthday check loop stopped")
        raise

    except Exception as exc:
        log.exception("[BIRTHDAY] Error in check loop: %s", exc)


# ============================================================================
# EVENT HANDLERS
# ============================================================================

async def on_muc_presence(bot, pres):
    """Called when someone joins a MUC room."""
    try:
        if pres["type"] == "unavailable":
            return

        room_jid = pres["from"].bare
        nick = pres["from"].resource

        jid = pres["muc"].get("jid")
        if not jid:
            return

        user_jid_str = str(jid.bare)

        enabled = await _is_enabled_for_room(bot, "birthday_notify",
                                             "birthday_notify", str(room_jid))
        if not enabled:
            return

        await _check_user_birthday(bot, user_jid_str, nick, room_jid)

    except Exception as exc:
        log.exception("[BIRTHDAY] Error in muc_presence: %s", exc)


# ============================================================================
# COMMANDS
# ============================================================================

@command("birthday_notify", role=Role.USER)
async def birthday_notify_command(bot, sender_jid, nick, args, msg, is_room):
    """Enable, disable, or show birthday notifications for this room."""
    subcmd = args[0].lower() if args else None

    if await handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=_get_birthday_store,
        key="birthday_notify",
        label="Birthday notifications",
        storage="dict",
        log_prefix="[BIRTHDAY]",
    ):
        if (
            subcmd == "on"
            and not is_room
            and str(msg["from"].bare) in JOINED_ROOMS
        ):
            room_jid = str(msg["from"].bare)
            asyncio.create_task(_check_room_birthdays(bot, room_jid))
        return

    prefix = config.get("prefix", ",")
    bot.reply(msg, f"ℹ️ Usage: {prefix}birthday_notify on|off|status")


async def _get_birthday_store(bot):
    return bot.db.users.plugin("birthday_notify")


# ============================================================================
# PLUGIN LIFECYCLE
# ============================================================================

async def on_ready(bot):
    """Called when bot is fully initialized."""
    global _BIRTHDAY_CHECK_TASK

    try:
        log.info("[BIRTHDAY] Initializing birthday notifications...")

        # Avoid duplicate tasks on reload/re-ready.
        if _BIRTHDAY_CHECK_TASK and not _BIRTHDAY_CHECK_TASK.done():
            _BIRTHDAY_CHECK_TASK.cancel()
            try:
                await _BIRTHDAY_CHECK_TASK
            except asyncio.CancelledError:
                pass

        _BIRTHDAY_CHECK_TASK = asyncio.create_task(_birthday_check_loop(bot))

        log.info(
            "[BIRTHDAY] ✅ Birthday notification system ready; "
            "first scan scheduled in %ss",
            INITIAL_SCAN_DELAY_SECONDS,
        )

    except Exception as exc:
        log.exception("[BIRTHDAY] Error during initialization: %s", exc)


async def on_load(bot):
    """Called when plugin is loaded."""
    try:
        bot.bot_plugins.register_event(
            "birthday_notify",
            "groupchat_presence",
            partial(on_muc_presence, bot),
        )
        log.info("[BIRTHDAY] ✅ MUC presence handler registered")

    except Exception as exc:
        log.exception("[BIRTHDAY] Error registering event handler: %s", exc)


async def on_unload(bot):
    """Called when plugin is unloaded."""
    global _BIRTHDAY_CHECK_TASK

    try:
        if _BIRTHDAY_CHECK_TASK:
            _BIRTHDAY_CHECK_TASK.cancel()
            try:
                await _BIRTHDAY_CHECK_TASK
            except asyncio.CancelledError:
                pass

        _BIRTHDAY_CHECK_TASK = None

        log.info("[BIRTHDAY] ✅ Birthday notification plugin unloaded")

    except Exception as exc:
        log.exception("[BIRTHDAY] Error during plugin unload: %s", exc)
