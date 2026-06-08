# plugins/core.py

"""
Core utility and shared helpers for all envsbot plugins.
Depends on essential plugins (e.g., "rooms") via PLUGIN_META.

Put any functions or objects here that:
  - are needed by multiple plugins
  - require access to JOINED_ROOMS or runtime bot/plugin state
  - should ONLY be initialized after their dependencies are loaded
"""
import logging
import re
import pytz
import datetime
from slixmpp import JID
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Optional

from utils.command import Role

from plugins.rooms import JOINED_ROOMS

PLUGIN_META = {
    "name": "_core",
    "version": "0.4.0",
    "description": "Core utilities and shared helpers for other plugins.",
    "category": "internal",
    "requires": ["rooms"],  # Ensure 'rooms' is loaded first
    "hidden": True,         # Optional: Hide from user plugin listings
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Check if a message is a MUC private message
# (i.e., a direct message from a MUC participant to the bot)
# ---------------------------------------------------------------------
def _is_muc_pm(msg, joined_rooms=None):
    """Return True if message is a MUC private message."""
    # Joined rooms can be passed or imported if not given
    if not joined_rooms:
        joined_rooms = JOINED_ROOMS
    muc_from = getattr(msg["from"], "bare", None)
    return (
        msg["type"] in ("chat", "normal")
        and muc_from in joined_rooms
        and getattr(msg["from"], "resource", None) is not None
    )


# ----------------------------------------------------------------------
# Check if a message is a public groupchat message (i.e., sent to a MUC
# room, not a private message)
# -----------------------------------------------------------------------
def _is_public_muc(msg, is_room: bool) -> bool:
    return is_room and msg.get("type") == "groupchat"


# ----------------------------------------------------------------------
# Helper to normalize a JID to its bare form, with robust error handling to
# avoid exceptions from invalid JID formats. If the input is not a valid JID,
# it will fall back to a best-effort string parsing to extract the bare JID.
# -----------------------------------------------------------------------
def _normalize_bare_jid(value) -> str | None:
    if not value:
        return None
    try:
        return str(JID(str(value)).bare)
    except Exception:
        value = str(value)
        return value.split("/", 1)[0]


# -----------------------------------------------------------------------
# Get the real JID of the sender, check for MUC private message first,
# then groupchat, then DM
# ----------------------------------------------------------------------
async def get_real_jid(bot, msg):
    """
    Resolve the real sender JID in all contexts (groupchat, MUC PM, or DM).

    returns:
        - jid (str): The resolved JID of the sender (normalized)
        - is_muc_private (bool): True if this was a MUC private message
        - is_muc_groupchat (bool): True if this was a groupchat message
    """
    jid = None
    is_muc_private = False
    is_muc_groupchat = False

    muc = bot.plugin.get("xep_0045", None)
    result = None
    if muc:
        room = getattr(msg["from"], "bare", None)
        nick = getattr(msg["from"], "resource", None)
        # log.info(
        #     "[CORE] Resolving real JID for room: %s, nick: %s", room, nick
        # )
        try:
            result = (
                JOINED_ROOMS.get(room, {})
                .get("nicks", {})
                .get(nick, {})
                .get("jid", None)
            )
        except Exception:
            # log.warning(
            #     "[CORE] 🟡 Error resolving real JID for %s in %s: %s",
            #     nick, room, e
            # )
            result = None

        # Fallback: try to resolve via UserManager's _nick_index if not found
        if result is None and nick:
            result = await get_jids_from_nick_index(bot, nick)

    if result is not None and _is_muc_pm(msg):
        # MUC private message, try to resolve real JID
        jid = result
        is_muc_private = True
    elif result is not None and msg["type"] == "groupchat":
        # Groupchat message, use the resolved JID
        jid = result
        is_muc_groupchat = True
    elif msg["to"].bare == bot.boundjid.bare:
        # Direct message to the bot, use the sender's JID
        jid = msg["from"].bare
    else:
        # Fallback: use the sender's JID as-is
        jid = None
    return _normalize_bare_jid(jid), is_muc_private, is_muc_groupchat


# -----------------------------------------------------------------------
# Helper to ensure a user row exists in the database for a given JID, creating
# one if necessary. This is useful to call at the start of plugin commands
# handlers to ensure we have a user record to work with.
# -----------------------------------------------------------------------
async def _ensure_user_exists(bot, user_jid: str, nickname: str | None = None):
    user_jid = str(user_jid)

    existing = await bot.db.users.get(user_jid)
    if existing is not None:
        return

    try:
        await bot.db.users.create(user_jid, nickname)
        log.debug("[CORE] Created user row for %s", user_jid)
    except Exception:
        existing = await bot.db.users.get(user_jid)
        if existing is None:
            raise


# ------------------------------------------------------------------------
# Helper to get a user's timezone from their vCard, with robust error handling
# and fallback to UTC if anything goes wrong (e.g., no vCard, missing TIMEZONE
# field, invalid timezone name, database errors).
# ------------------------------------------------------------------------

# RETURN the tzinfo object for a user's timezone, or UTC if not set/invalid
async def get_user_tzinfo(bot, timezone_jid: str) -> datetime.tzinfo:
    """
    Return the user's timezone as a tzinfo object, or UTC if not set/invalid.
    """
    tzname = await _get_user_timezone(bot, timezone_jid)
    try:
        return pytz.timezone(tzname)
    except Exception:
        log.warning(
            "[CORE] Invalid timezone for %s: %s; falling back to UTC",
            timezone_jid,
            tzname,
        )
        return pytz.timezone("UTC")


# Get IANA timezone name from the user's vCard TIMEZONE field as a string
async def _get_user_timezone(bot, timezone_jid: str | None) -> str:
    """Return the user's vCard TIMEZONE or UTC as fallback."""
    if not timezone_jid:
        return str(pytz.timezone("UTC"))

    try:
        store = bot.db.users.plugin("vcard")
        timezone_name = await store.get(str(timezone_jid), "TIMEZONE")

        if timezone_name and timezone_name in pytz.all_timezones:
            return str(pytz.timezone(timezone_name))

        if timezone_name:
            log.warning(
                "[CORE] Invalid vCard TIMEZONE for %s: %s; falling back to"
                " UTC",
                timezone_jid,
                timezone_name,
            )

    except Exception as exc:
        log.warning(
            "[CORE] Could not read vCard TIMEZONE for %s: %s; falling back to"
            " UTC",
            timezone_jid,
            exc,
        )

    return str(pytz.timezone("UTC"))


# ------------------------------------------------------------------------
# Helpers for managing room-scoped plugin settings, stored in the global
# store as a dict of {room_jid: True}.
# -------------------------------------------------------------------------
async def _get_enabled_rooms(bot, key, plugin) -> dict:
    """
    Return a dict of {room_jid: True} for rooms where the feature is enabled.
    """
    store = await get_plugin_store(bot, plugin)
    data = await store.get_global(key, default={})
    return data if isinstance(data, dict) else {}


async def _is_enabled_for_room(bot, key, plugin, room_jid: str) -> bool:
    """Return True if the feature is enabled for the given room_jid, based on
    the dict of {room_jid: True} stored in the global store under the given
    key.
    """
    enabled = await _get_enabled_rooms(bot, key, plugin)
    return bool(enabled.get(room_jid))


async def get_plugin_store(bot, plugin):
    return bot.db.users.plugin(plugin)


# ------------------------------------------------------------------------
# General helper to check if a plugin feature is enabled for a room, based on a
# dict of {room_jid: True} stored in the plugin's global store under a given
# key. This is a more flexible version of _is_enabled_for_room that can be used
# by any plugin without needing to import the plugin module or pass the plugin
# instance, by allowing the caller to provide a custom store_getter function
# that knows how to access the plugin's global store.global
# -------------------------------------------------------------------------
StoreGetter = Callable[[Any], Awaitable[Any]]


async def is_plugin_enabled_for_room(
    bot,
    store_getter: StoreGetter,
    key: str,
    room_jid: str,
) -> bool:
    """Return True if {key} enabled for room_jid in the plugin's global store.
    """
    store = await store_getter(bot)
    state = await store.get_global(key, default={})
    return isinstance(state, dict) and bool(state.get(room_jid))


# ------------------------------------------------------------------------
# Helper to parse duration strings like "2d5h3m20s" into total seconds.
# Supports individual units (e.g., "10m") as well as combined formats. Returns
# None for invalid formats or zero duration.
# ------------------------------------------------------------------------
def parse_duration(duration_str: str) -> int | None:
    """Parse a duration string to seconds.

    Supports:
    - Single formats: 10s, 5m, 1h, 2d
    - Combined formats: 2d5h3m20s, 1h30m, 3d12h
    """
    if not duration_str:
        return None

    duration_str = duration_str.lower().strip()

    pattern = r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?"
    match = re.fullmatch(pattern, duration_str)

    if not match:
        return None

    days, hours, minutes, seconds = match.groups()

    if not any([days, hours, minutes, seconds]):
        return None

    total_seconds = (
        (int(days) if days else 0) * 86400
        + (int(hours) if hours else 0) * 3600
        + (int(minutes) if minutes else 0) * 60
        + (int(seconds) if seconds else 0)
    )

    return total_seconds if total_seconds > 0 else None


# -----------------------------------------------------------------------
# Helper to look up real JIDs from the UserManager's _nick_index, which is
# populated by the MUC plugin when users join rooms. This allows us to resolve
# real JIDs from nicks in MUC contexts, even if we don't have the full message
# context.
# -----------------------------------------------------------------------
async def get_jids_from_nick_index(bot, nick):
    """Look up the real JID of a nick from the UserManager's _nick_index."""
    idx = getattr(bot.db.users, "_nick_index", {})
    value = idx.get(nick)
    if isinstance(value, set):
        return next(iter(value), None)
    if isinstance(value, list):
        return value
    return value or None


# -----------------------------------------------------------------------
# Helper to look up the real JID of a MUC occupant from JOINED_ROOMS,
# given a message context
# -----------------------------------------------------------------------
async def get_real_jid_from_occupant(bot, msg, nick=None):
    """Look up the real JID of a nick from room occupant"""
    try:
        nicks = JOINED_ROOMS.get(msg["from"].bare, {}).get("nicks", {})
        if nick is None:
            jid = nicks.get(msg["from"].resource, {}).get("jid", None)
        else:
            jid = nicks.get(nick, {}).get("jid", None)
    except Exception as e:
        s = "[CORE] 🟡 Error resolving real JID from occupant for"
        s += "%s in %s: %s", msg["from"].resource, msg["from"].bare, e
        log.warning(s)
        jid = None
    return jid


# -----------------------------------------------------------------------
# Helper to look up all nicks of a JID from the UserManager's _nick_index,
# which is populated by the MUC plugin when users join rooms. This allows
# us to find all nicks associated with a JID across different rooms and
# contexts.
# -----------------------------------------------------------------------
async def get_nicks_from_jid(bot, jid):
    """
    Helper to look up all nicknames of a JID from the
    UserManager's _nick_index. Returns a list of nicks.
    """
    idx = getattr(bot.db.users, "_nick_index", {})
    nicks = []
    for nick, value in idx.items():
        if isinstance(value, set) and jid in value:
            nicks.append(nick)
        elif isinstance(value, list) and jid in value:
            nicks.append(nick)
        elif value == jid:
            nicks.append(nick)
    return nicks


# -----------------------------------------------------------------------
# Helper to check if a user exists in the database, and reply with an error
# -----------------------------------------------------------------------
async def _check_user_exists(bot, sender_jid, msg):
    """
    Check if the user exists in the database.

    Args:
        bot: The bot instance.
        sender_jid: The JID to check.
        msg: The message object.

    Returns:
        bool: True if user exists, False otherwise.
    """
    jid = str(sender_jid)
    user = await bot.db.users.get(jid)
    if not user:
        log.warning(
            "[CORE] 🔴  Unregistered user tried to access: %s", jid
        )
        bot.reply(msg, "🔴  You are not a registered user.")
        return False
    return True


# ------------------------------------------------------------------------
# Shared paging helper
# ------------------------------------------------------------------------
def paginate_items(items: list[Any], page: int, page_size: int):
    """Paginate a list and clamp page into a valid range.

    Returns:
        (page_items, page, total_pages, total_items)
    """
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], page, total_pages, total


# ------------------------------------------------------------------------
# Shared stanza/message cache helpers
# Namespaced so plugins can share the same infrastructure without sharing
# the same data bucket or eviction policy.
# ------------------------------------------------------------------------
_SHARED_MESSAGE_CACHES: dict[str, dict[str, deque]] = defaultdict(
    lambda: defaultdict(lambda: deque(maxlen=10))
)
_SHARED_PROCESSED_STANZAS: dict[str, set[str]] = defaultdict(set)
_SHARED_PROCESSED_STANZA_ORDER: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=10000)
)


def get_stanza_id(msg) -> str | None:
    """Extract a stable message id from a stanza."""
    try:
        stanza_id = msg.get("stanza_id")
        if stanza_id:
            value = stanza_id.get("id")
            if value:
                return str(value)
    except Exception:
        pass

    try:
        msg_id = msg.get("id")
        if msg_id:
            return str(msg_id)
    except Exception:
        pass

    return None


def remember_stanza(namespace: str, stanza_id: str | None) -> bool:
    """Return False if stanza was already processed in this namespace."""
    if not stanza_id:
        return True

    processed = _SHARED_PROCESSED_STANZAS[namespace]
    order = _SHARED_PROCESSED_STANZA_ORDER[namespace]

    if stanza_id in processed:
        return False

    if len(order) == order.maxlen:
        old = order.popleft()
        processed.discard(old)

    processed.add(stanza_id)
    order.append(stanza_id)
    return True


def get_reply_target(msg) -> str | None:
    """Get the ID of the message this is a reply to."""
    try:
        if "reply" in msg:
            reply = msg.get("reply")
            if reply:
                value = reply.get("id")
                if value:
                    return str(value)
    except Exception:
        pass

    return None


def extract_reply_quote(body: str) -> str | None:
    """Extract the original message from a reply quote."""
    if not body:
        return None

    lines = body.strip().splitlines()
    quoted_lines = []

    for line in lines:
        if line.startswith(">"):
            quoted_lines.append(line[2:] if len(line) > 1 else "")
        else:
            break

    text = "\n".join(quoted_lines).strip()
    return text or None


def cache_message(
    namespace: str,
    room: str,
    nick: str | None,
    body: str,
    stanza_id: str | None,
    *,
    maxlen: int = 10,
    extra: dict[str, Any] | None = None,
):
    """Add a message to the shared cache for a namespace/room."""
    room_cache = _SHARED_MESSAGE_CACHES[namespace]

    if room not in room_cache or room_cache[room].maxlen != maxlen:
        room_cache[room] = deque(room_cache.get(room, []), maxlen=maxlen)

    entry = {
        "nick": nick,
        "body": body,
        "stanza_id": stanza_id,
    }

    if extra:
        entry.update(extra)

    room_cache[room].append(entry)


def get_cached_messages(namespace: str, room: str) -> list[dict[str, Any]]:
    """Return cached messages for a namespace/room."""
    return list(_SHARED_MESSAGE_CACHES[namespace][room])


def get_last_cached_message(
    namespace: str, room: str
) -> dict[str, Any] | None:
    """Return the last cached message entry for a namespace/room."""
    cache = _SHARED_MESSAGE_CACHES[namespace][room]
    if not cache:
        return None
    return cache[-1]


def get_cached_message_by_id(
    namespace: str, room: str, msg_id: str
) -> dict[str, Any] | None:
    """Return a cached message entry by stanza_id for a namespace/room."""
    cache = _SHARED_MESSAGE_CACHES[namespace][room]
    if not cache:
        return None

    for entry in cache:
        if entry.get("stanza_id") == msg_id:
            return entry

    return None


# ------------------------------------------------------------------------
# Plugin helper for handling room-scoped on/off/status commands in MUC private
# messages. This is a common pattern for plugins that have features which can
# be enabled or disabled on a per-room basis, and we want to allow room admins
# to control these settings via simple commands in the MUC DM.
# ------------------------------------------------------------------------

_CONTROL_COMMANDS = {"on", "off", "status"}
_ADMIN_AFFILIATIONS = {"admin", "owner"}


def _room_and_nick_from_muc_pm(msg):
    """Return (room_jid, nick) for a MUC private message."""
    from_jid = msg["from"]
    return str(from_jid.bare), str(from_jid.resource or "")


def _get_muc_occupant(room_jid: str, nick: str) -> Optional[dict]:
    """Return cached occupant info from JOINED_ROOMS, if available."""
    room_data = JOINED_ROOMS.get(room_jid)

    if not room_data:
        return None

    return room_data.get("nicks", {}).get(nick)


async def is_room_moderator_or_admin(
    bot,
    room_jid: str,
    nick: str,
) -> bool:
    """
    True if the occupant is admin/owner by MUC affiliation OR is a
    moderator/admin by the bot's room-scoped role mapping
    (fallback via real JID).
    """
    occupant = _get_muc_occupant(room_jid, nick)
    if not occupant:
        return False

    affiliation = str(occupant.get("affiliation") or "").lower()
    if affiliation in {"admin", "owner"}:
        return True

    real_jid = occupant.get("jid")
    if real_jid:
        try:
            role = await bot.get_user_role(str(real_jid), room_jid)
            return role <= Role.MODERATOR
        except Exception:
            log.exception("[CORE] Failed to resolve user room role")

    return False


async def muc_pm_sender_can_manage_room(
    bot,
    msg,
    is_room: bool,
) -> tuple[bool, str, Optional[str]]:
    """Check whether the sender may manage room-scoped plugin settings.

    Returns:
        (allowed, room_jid, reason)
    """
    if is_room:
        return False, "", "ℹ️ This command can only be used in a MUC DM."

    room_jid, nick = _room_and_nick_from_muc_pm(msg)

    if room_jid not in JOINED_ROOMS:
        return False, room_jid, "ℹ️ This command can only be used in a MUC DM."

    occupant = _get_muc_occupant(room_jid, nick)

    if not occupant:
        return False, room_jid, "⛔ Could not verify your room permissions."

    affiliation = str(occupant.get("affiliation") or "").lower()

    if affiliation in _ADMIN_AFFILIATIONS:
        return True, room_jid, None

    real_jid = occupant.get("jid")

    if real_jid:
        try:
            role = await bot.get_user_role(str(real_jid), room_jid)

            if role <= Role.MODERATOR:
                return True, room_jid, None

        except Exception:
            log.exception("[CORE] Failed to resolve user role")

    return (
        False,
        room_jid,
        "⛔ Only room admins/owners can use on/off/status here.",
    )


def _format_status(label: str, enabled: bool) -> str:
    state = "enabled" if enabled else "disabled"
    icon = "✅" if enabled else "ℹ️"
    return f"{icon} {label} is **{state}** in this room."


def _format_enabled(label: str) -> str:
    return f"✅ {label} enabled in this room."


def _format_disabled(label: str) -> str:
    return f"✅ {label} disabled in this room."


def _format_already_enabled(label: str) -> str:
    return f"ℹ️ {label} already enabled."


def _format_already_disabled(label: str) -> str:
    return f"ℹ️ {label} already disabled."


async def handle_room_toggle_command(
    bot,
    msg,
    is_room: bool,
    args: list[str],
    *,
    store_getter: StoreGetter,
    key: str,
    label: str,
    storage: str = "dict",
    list_field: str = "rooms",
    log_prefix: str = "[PLUGIN]",
) -> bool:
    """Shared handler for `{plugin} on|off|status` commands.

    Returns True when args[0] is one of on/off/status and the command was fully
    handled. Returns False for all other subcommands so the plugin can continue
    normal handling.

    Supported storage formats:
    - storage="dict": {room_jid: True}
    - storage="list": {list_field: [room_jid, ...]}
    """
    # -----------------------------------------------------------
    # DELETED STORAGE TYPE 'list' to reduce cyclomatic complexity
    # -----------------------------------------------------------
    if not args:
        return False

    subcmd = str(args[0]).lower()

    if subcmd not in _CONTROL_COMMANDS:
        return False

    allowed, room_jid, reason = await muc_pm_sender_can_manage_room(
        bot,
        msg,
        is_room,
    )

    if not allowed:
        bot.reply(msg, reason)
        return True

    store = await store_getter(bot)

    if storage == "dict":
        state = await store.get_global(key, default={})

        if not isinstance(state, dict):
            state = {}

        enabled = bool(state.get(room_jid))

        if subcmd == "status":
            bot.reply(msg, _format_status(label, enabled))
            return True

        if subcmd == "on":
            if enabled:
                bot.reply(msg, _format_already_enabled(label))
                return True

            state[room_jid] = True
            await store.set_global(key, state)

            bot.reply(msg, _format_enabled(label))
            log.info("%s Room %s enabled", log_prefix, room_jid)
            return True

        if not enabled:
            bot.reply(msg, _format_already_disabled(label))
            return True

        state.pop(room_jid, None)
        await store.set_global(key, state)

        bot.reply(msg, _format_disabled(label))
        log.info("%s Room %s disabled", log_prefix, room_jid)
        return True

    raise ValueError(f"Unsupported room-toggle storage: {storage}")
