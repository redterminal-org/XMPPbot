"""
Room management and persistence.

This plugin provides administrative commands for managing XMPP
multi-user chat rooms stored in the bot database. Administrators
can add rooms, update their configuration, remove them, view the
current list of rooms, and control whether the bot joins or leaves
rooms at runtime.

Newly created rooms will be created with the plugin defaults (on/off)
defined in the "rooms" plugin.

You can set the rooms plugins back to the defaults with the following command:
    {prefix}room set_plugin_defaults

Rooms can optionally be configured with an *autojoin* flag so the
bot automatically joins them when it starts.
"""

import asyncio
import logging

from functools import partial

from utils.command import command, Role
from utils.config import config

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "rooms",
    "version": "0.2.5",
    "description": "Database-backed room management",
    "category": "core",
}

# joined rooms module global
JOINED_ROOMS = {}

# ------------------------------------------------
# Default Plugin Setup for rooms
#
# IMPORTANT NOTE: This only works for "type": "dict"
# ------------------------------------------------
PLUGIN_DEFAULTS = {
    "help": False,
    "birthday_notify": False,
    "ducks": False,
    "karma": False,
    "pin": True,
    "poll": False,
    "information": True,
    "dice": True,
    "tell": True,
    "tools": True,
    "reminder": True,
    "sed": True,
    "presence": True,
    "urlcheck": True,
    "vcard": True,
    "weather": True,
    "xkcd": False,
    "xmpp": True,
}
PLUGIN_STORE_CONFIG = {
    "help": {"type": "dict", "key": "HELP"},
    "birthday_notify": {"type": "dict", "key": "birthday_notify"},
    "ducks": {"type": "dict", "key": "DUCKS"},
    "karma": {"type": "dict", "key": "KARMA"},
    "pin": {"type": "dict", "key": "PIN"},
    "poll": {"type": "dict", "key": "POLL"},
    "information": {"type": "dict", "key": "INFORMATION"},
    "dice": {"type": "dict", "key": "DICE"},
    "tell": {"type": "dict", "key": "TELL"},
    "tools": {"type": "dict", "key": "TOOLS"},
    "reminder": {"type": "dict", "key": "REMINDER"},
    "sed": {"type": "dict", "key": "SED"},
    "presence": {"type": "dict", "key": "PRESENCE"},
    "urlcheck": {"type": "dict", "key": "URLCHECK"},
    "vcard": {"type": "dict", "key": "VCARD"},
    "weather": {"type": "dict", "key": "WEATHER"},
    "xkcd": {"type": "dict", "key": "XKCD"},
    "xmpp": {"type": "dict", "key": "XMPP"},
}
# ------------------------------------------------


# -------------------------------------------------
# Event Handlers
# -------------------------------------------------

# Handlers
async def on_muc_presence(bot, pres):
    try:
        room = pres["from"].bare
        nick = pres["from"].resource
        role = pres["muc"].get("role")
        jid = pres["muc"].get("jid")
        affiliation = pres["muc"].get("affiliation")

        if jid is None:
            jid = pres["from"]

        jid_bare = str(jid.bare) if jid else None

        # Defensive: Use .get() instead of direct access
        room_info = JOINED_ROOMS.get(room)
        if room_info is None:
            room_info = {
                "nick": "unknown",
                "autojoin": "unknown",
                "status": None,
                "affiliation": "unknown",
                "role": "unknown",
                "nicks": {}
            }

        if pres["type"] == "unavailable":
            if JOINED_ROOMS.get(room) is None:
                return
            if nick == JOINED_ROOMS.get(room, {}).get("nick"):
                JOINED_ROOMS.pop(room, None)
            else:
                try:
                    nicks = JOINED_ROOMS.get(room, {}).get("nicks", {})
                    if nick in nicks:
                        del nicks[nick]
                except Exception as e:
                    log.debug(f"[ROOMS] Error removing nick '{nick}' from '{room}': {e}")

        new_nick = room_info["nicks"].get(nick)
        if new_nick is None:
            new_nick = {
                "jid": jid_bare if jid is not None else str(pres["from"]),
                "affiliation":
                    affiliation if affiliation is not None else "unknown",
                "role": role if role is not None else "unknown"
            }
        if affiliation is not None:
            new_nick["affiliation"] = affiliation
        if role is not None:
            new_nick["role"] = role

        room_info["nicks"][nick] = new_nick

        if jid_bare == bot.boundjid.bare:
            if affiliation is not None:
                if affiliation != room_info["affiliation"]:
                    room_info["affiliation"] = affiliation
            if role != room_info["role"]:
                room_info["role"] = role
            if nick != room_info["nick"]:
                room_info["nick"] = nick

        JOINED_ROOMS[room] = room_info

    except Exception as e:
        log.exception(f"[ROOMS] Error in on_muc_presence: {e}")


# -------------------------------------------------
# ON_LOAD startup function (Module autoloadind)
# -------------------------------------------------

async def on_load(bot):

    # --- add event handlers ---
    bot.bot_plugins.register_event(
        "rooms",
        "groupchat_presence",
        partial(on_muc_presence, bot))

    # get muc and rooms_db with guard
    muc = bot.plugin["xep_0045"]
    rooms_db = bot.db.rooms
    if muc is None or rooms_db is None:
        log.warning("[ROOMS] 🟡️ missing dependencies: "
                    f"rooms_db={'OK' if rooms_db is not None else 'missing'} "
                    f"xep_0045={'OK' if muc is not None else 'missing'}")
        return

    # Case 1: reload → restore previous runtime state
    reload_rooms = getattr(bot, "_reload_rooms", None)

    if reload_rooms is not None:
        del bot._reload_rooms

        for room, data in reload_rooms.items():
            # --- Get room data from DB ---
            db_room = await rooms_db.get(room)
            if db_room:
                _, db_nick, db_autojoin, db_status = db_room
            else:
                db_nick = None
                db_autojoin = None
                db_status = None

            # --- Runtime truth from slixmpp
            raw_nick = (data.get("nick")
                        or db_nick
                        or config.get("nick")
                        or "envsbot")
            nick = str(raw_nick)

            # Use runtime state if available, fallback to DB
            autojoin = data.get("autojoin")
            if autojoin is None:
                autojoin = db_autojoin

            status = data.get("status") or db_status or None

            # --- rebuild runtime state ---
            JOINED_ROOMS[room] = {
                "nick": nick,
                "autojoin": autojoin,
                "status": status,
                "affiliation": "unknown",
                "role": "unknown",
                "nicks": {}
            }

            await muc.join_muc(
                room,
                nick,
                pshow=bot.presence.status["show"],
                pstatus=bot.presence.status["status"]
            )

            bot.presence.joined_rooms[room] = nick
    else:
        # Case 2: normal startup → use config
        await autojoin_rooms(bot)


# -------------------------------------------------
# ON_UNLOAD teardown function.
# -------------------------------------------------

async def on_unload(bot):
    bot._reload_rooms = dict(JOINED_ROOMS)

    for room_jid, data in JOINED_ROOMS.items():
        bot.plugin["xep_0045"].leave_muc(room_jid, data["nick"])

    bot.presence.joined_rooms.clear()


# -------------------------------------------------
# ROOM PRIVILEGE CHECK
# -------------------------------------------------

def bot_has_privilege(room, required=("admin", "owner")):
    info = JOINED_ROOMS.get(room)
    if not info:
        return False
    return info.get("affiliation") in required


# -------------------------------------------------
# ROOM JID VALIDATION
# -------------------------------------------------

async def is_valid_muc_domain(bot, domain: str) -> bool:
    """
    Check if a domain provides a MUC service using XMPP service discovery.
    """

    try:
        info = await bot["xep_0030"].get_info(jid=domain)

        for feature in info["disco_info"]["features"]:
            if feature == "http://jabber.org/protocol/muc":
                return True

    except Exception as e:
        log.warning("[ROOMS] 🟡️ MUC discovery failed for %s: %s", domain, e)

    return False


async def is_valid_room_jid(bot, jid: str, msg) -> bool:
    """
    Validate that a string looks like a proper room JID.

    Requirements
    ------------
    - must contain node@domain
    - must not contain a resource part
    """

    if "/" in jid:
        return False

    if "@" not in jid:
        return False

    node, domain = jid.split("@", 1)

    if not node or not domain:
        return False

    try:
        async with asyncio.timeout(5):
            is_valid = await is_valid_muc_domain(bot, domain)
    except TimeoutError:
        is_valid = False
    if not is_valid:
        bot.reply(
            msg,
            f"🟡️ Domain '{domain}' does not provide muc service.")
        return False
    return True


# -------------------------------------------------
# ROOM STATUS HELPER FUNCTIONS
# -------------------------------------------------
async def room_status_get(bot, room_jid, path=None):
    return await bot.db.rooms.status_get(room_jid, path)


async def room_status_set(bot, room_jid, path, value):
    await bot.db.rooms.status_set(room_jid, path, value)


async def room_status_delete(bot, room_jid, path):
    await bot.db.rooms.status_delete(room_jid, path)


# -------------------------------------------------
# AutoJoin Rooms function
# -------------------------------------------------

async def autojoin_rooms(bot):
    """
    Join all rooms marked with autojoin in the database.
    """
    # get muc and rooms_db with guard
    muc = bot.plugin["xep_0045"]
    rooms_db = bot.db.rooms
    if muc is None or rooms_db is None:
        log.warning("[ROOMS] 🟡️ missing dependencies: "
                    f"rooms_db={'OK' if rooms_db is not None else 'missing'} "
                    f"xep_0045={'OK' if muc is not None else 'missing'}")
        return

    rows = await rooms_db.list()
    for room_jid, nick, autojoin, status in rows:
        if not autojoin:
            continue
        log.info("[MUC] Autojoining room %s as %s", room_jid, nick)
        try:
            await muc.join_muc(
                room_jid,
                nick,
                pshow=bot.presence.status["show"],
                pstatus=bot.presence.status["status"])

            room_info = JOINED_ROOMS.get(room_jid)

            if room_info:
                # ✅ partial update (DO NOT overwrite runtime data)
                room_info["autojoin"] = autojoin
                room_info["status"] = status

                # optional: update nick if you trust DB more
                # room_info["nick"] = nick

            else:
                # ✅ full create (first time)
                JOINED_ROOMS[room_jid] = {
                    "nick": nick,
                    "autojoin": autojoin,
                    "status": status,
                    "affiliation": "unknown",
                    "role": "unknown",
                    "nicks": {}
                }
                bot.presence.joined_rooms[room_jid] = nick
        except Exception:
            log.exception(f"[ROOMS] 🔴 Couldn't join room '{room_jid}'")


# -------------------------------------------------
# Set Room Control Defaults (for plugins that use room control)
# -------------------------------------------------
async def set_room_control_defaults(bot, room_jid, defaults=None):
    """
    Reset all plugin room controls to their configured defaults.

    Important:
    The storage key is not always the plugin name. Use the configured
    PLUGIN_STORE_CONFIG[plugin]["key"] for get_global/set_global.
    """
    if defaults is None:
        defaults = PLUGIN_DEFAULTS

    for plugin, should_enable in defaults.items():
        conf = PLUGIN_STORE_CONFIG[plugin]
        typ = conf["type"]
        key = conf["key"]
        store = bot.db.users.plugin(plugin)

        if typ == "dict":
            state = await store.get_global(key, default={})
            if not isinstance(state, dict):
                state = {}

            if should_enable:
                state[room_jid] = True
            else:
                state.pop(room_jid, None)

            log.info(f"[ROOMS][DICT] Setting defaults for plugin '{plugin}' key '{key}': {state}")
            await store.set_global(key, state)

        elif typ == "list":
            list_field = conf.get("list_field", "rooms")
            state = await store.get_global(key, default={list_field: []})
            if not isinstance(state, dict):
                state = {list_field: []}

            rooms = state.get(list_field, [])
            if not isinstance(rooms, list):
                rooms = []

            if should_enable:
                if room_jid not in rooms:
                    rooms.append(room_jid)
            else:
                if room_jid in rooms:
                    rooms.remove(room_jid)

            state[list_field] = rooms

            log.info(f"[ROOMS][LIST] Setting defaults for plugin '{plugin}' key '{key}': {rooms}")
            await store.set_global(key, state)

        else:
            raise ValueError(f"Unsupported storage type: {typ} for plugin {plugin}")


# -------------------------------------------------
# ROOMS SETDEFAULTS
# -------------------------------------------------
@command("rooms set_plugin_defaults", role=Role.MODERATOR,
         aliases=["room set_plugin_defaults",
                  "rooms spd", "room spd"])
async def cmd_room_setdefaults(bot, sender_jid, nick, args, msg, is_room):
    """
    Reset all room plugins to the defaults for the current room.

    Usage:
        {prefix}room set_plugin_defaults
        {prefix}room spd
    """
    if is_room:
        bot.reply(msg, "🔴 This command can only be used in MUC PMs. to the bot.")
        return
    if len(args) != 0:
        bot.reply(msg, f"🟡️ Usage: {bot.prefix}room set_plugin_defaults")
        return
    room_jid = msg['from'].bare
    if room_jid not in JOINED_ROOMS:
        bot.reply(msg, f"🔴 Room '{room_jid}' is not currently joined. Please join the room first before setting defaults.")
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not joined for setdefaults!")
        return

    room = await bot.db.rooms.get(room_jid)
    if not room:
        bot.reply(msg, f"🔴 Room '{room_jid}' does not exist in the database.")
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not found in DB for setdefaults!")
        return
    try:
        await set_room_control_defaults(bot, room_jid)
        bot.reply(msg, f"✅ Restored plugin defaults for room '{room_jid}'.")
        log.info(f"[ROOMS] ✅ Restored plugin defaults for room '{room_jid}'.")
    except Exception as e:
        bot.reply(msg, f"🔴 Error restoring defaults: {e}")
        log.exception(f"[ROOMS] 🔴 Error restoring defaults for room '{room_jid}': {e}")


# -------------------------------------------------
# ROOMS PLUGINS
# -------------------------------------------------
@command("rooms plugins", role=Role.MODERATOR, aliases=["room plugins"])
async def cmd_room_plugins(bot, sender_jid, nick, args, msg, is_room):
    """
    Show recent plugin setup for current room.

    Usage: {prefix}room plugins
    """
    if is_room:
        bot.reply(msg,
                  "🔴 This command can only be used in MUC PMs to the bot.")
        return
    if len(args) != 0:
        bot.reply(msg, f"🟡️ Usage: {bot.prefix}room plugins")
        return
    room_jid = msg['from'].bare
    if room_jid not in JOINED_ROOMS:
        bot.reply(msg, f"🔴 Room '{room_jid}' is not currently joined. Please join the room first to view plugin settings.")
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not joined for plugins command!")
        return

    lines = [f"📋 Plugin settings for room '{room_jid}'"]
    for plugin, should_enable in PLUGIN_DEFAULTS.items():
        conf = PLUGIN_STORE_CONFIG[plugin]
        typ = conf["type"]
        key = conf["key"]
        store = bot.db.users.plugin(plugin)

        if typ == "dict":
            state = await store.get_global(key, default={})
            if not isinstance(state, dict):
                state = {}

            line = f"• {plugin}: {'enabled' if state.get(room_jid) else 'disabled'}"
            line += "  |  Default: "
            default = "on" if PLUGIN_DEFAULTS.get(plugin, False) else "off"
            line += default
            modified = (PLUGIN_DEFAULTS.get(plugin, False) != bool(state.get(room_jid)))
            line += " (modified)" if modified else ""
            lines.append(line)

        elif typ == "list":
            list_field = conf.get("list_field", "rooms")
            state = await store.get_global(key, default={list_field: []})
            if not isinstance(state, dict):
                state = {list_field: []}

            rooms = state.get(list_field, [])
            if not isinstance(rooms, list):
                rooms = []

            line = f"• {plugin}: {'enabled' if room_jid in rooms else 'disabled'}"
            line += "  |  Default: "
            default = "on" if PLUGIN_DEFAULTS.get(plugin, False) else "off"
            line += default
            modified = (PLUGIN_DEFAULTS.get(plugin, False) != (room_jid in rooms))
            line += " (modified)" if modified else ""
            lines.append(line)

        else:
            raise ValueError(f"Unsupported storage type: {typ} for plugin {plugin}")

    log.info(f"[ROOMS] Displaying plugin settings for room '{room_jid}'")
    bot.reply(msg, "\n".join(lines))


# -------------------------------------------------
# ROOMS ADD
# -------------------------------------------------

@command("rooms add", role=Role.ADMIN, aliases=["room add"])
async def rooms_add(bot, sender_jid, nick, args, msg, is_room):
    """
    Add a new room configuration to the database. Doesn't join immediately!

    Command
    -------
    {prefix}rooms add <room_jid> <nick> [autojoin]

    Description
    -----------
    Registers a room together with the nickname the bot should use
    when joining it.

    If the optional *autojoin* flag is enabled, the bot will join
    the room automatically during startup.

    Examples
    --------
    {prefix}rooms add dev@conference.example.org BotNick
    {prefix}rooms add dev@conference.example.org BotNick true
    """

    if len(args) < 2 or len(args) > 3:
        bot.reply(
            msg,
            (f"🟡️ Usage: {bot.prefix}rooms add <room_jid>"
             " <nick> [autojoin]"),
            )
        return

    room_jid = args[0]
    room_nick = args[1]

    if not await is_valid_room_jid(bot, room_jid, msg):
        bot.reply(
            msg,
            f"🟡️ Invalid room JID: {room_jid}"
        )
        log.warning(f"[ROOMS]🟡️ Room '{room_jid}' not valid!")
        return

    autojoin = len(args) >= 3 and args[2].lower() in ("true", "1", "yes")

    db_room = await bot.db.rooms.get(room_jid)
    if not db_room:
        await bot.db.rooms.add(room_jid, room_nick, autojoin)

        log.info("[ROOMS] ➕ Added room %s nick=%s autojoin=%s",
                 room_jid, room_nick, autojoin)
        try:
            await set_room_control_defaults(bot, room_jid)
            log.info(f"[ROOMS] ✅ Set plugin defaults for new room '{room_jid}'.")
            bot.reply(msg, f"✅ Room added: {room_jid}. Plugin defaults set.")
        except Exception as e:
            log.exception(f"[ROOMS] 🔴 Error setting plugin defaults for new room '{room_jid}': {e}")
            bot.reply(msg, f"🔴 Error setting plugin defaults: {e}")
        return

    bot.reply(msg, f"[ROOMS] 🔴 Room already exists: {room_jid}")


# -------------------------------------------------
# ROOMS UPDATE
# -------------------------------------------------

@command("rooms update", role=Role.ADMIN, aliases=["room update"])
async def rooms_update(bot, sender_jid, nick, args, msg, is_room):
    """
    Update a configuration field of a stored room.

    Command
    -------
    {prefix}rooms update <room_jid> <field> <value>

    Supported fields
    ----------------
    nick
        Nickname the bot should use when joining the room.
    autojoin
        Controls whether the bot automatically joins the room
        when it starts.

        Allowed values:
        true, false, yes, no, 1, 0
    """

    if len(args) != 3:
        bot.reply(
            msg,
            (f"🟡️ Usage: {bot.prefix}rooms update <room_jid>"
             f" <field> <value>"),
        )
        return

    room_jid = args[0]

    if not await is_valid_room_jid(bot, room_jid, msg):
        bot.reply(
            msg,
            f"🟡️ Invalid room JID: {room_jid}",
        )
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not valid!")
        return

    field = args[1].lower()
    value = args[2]
    if field in ["nick", "autojoin"]:

        if field == "autojoin":
            value = value.lower() in ("true", "1", "yes")

        await bot.db.rooms.update(room_jid, **{field: value})

        log.info("[ROOMS] 🔧 Updated %s: %s=%s", room_jid, field, value)

        bot.reply(
            msg,
            f"🔧 Room updated: {room_jid}",
        )
    else:
        log.info("[ROOMS] 🔧 Update failed! Invalid field '%s'", field)

        bot.reply(
            msg,
            f"🔧 Room not updated. Invalid field: '{field}'",
        )


# -------------------------------------------------
# ROOMS DELETE
# -------------------------------------------------

@command("rooms delete", role=Role.ADMIN, aliases=["room delete"])
async def rooms_delete(bot, sender_jid, nick, args, msg, is_room):
    """
    Remove a room configuration from the database.

    Command
    -------
    {prefix}rooms delete <room_jid> [force]

    Description
    -----------
    Deletes a stored room configuration.

    If the bot is currently joined to that room it will leave it
    automatically.
    """

    if len(args) < 1:
        bot.reply(
            msg,
            f"🟡️ Usage: {bot.prefix}rooms delete <room_jid>",
        )
        return

    room_jid = args[0]

    if not await is_valid_room_jid(bot, room_jid, msg):
        bot.reply(
            msg,
            f"🟡️ Invalid room JID: {room_jid}",
        )
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not valid!")
        return

    try:
        db_room = await bot.db.rooms.get(room_jid)
        if db_room:
            await bot.db.rooms.delete(room_jid)

        joined = room_jid in JOINED_ROOMS

        if joined:
            room_data = JOINED_ROOMS.get(room_jid)
            if room_data:
                nick_to_leave = room_data.get("nick")
                if nick_to_leave:
                    try:
                        bot.plugin["xep_0045"].leave_muc(room_jid,
                                                         nick_to_leave)
                    except Exception as e:
                        log.warning(f"[ROOMS] Error leaving room: {e}")

            JOINED_ROOMS.pop(room_jid, None)

            if room_jid in bot.presence.joined_rooms:
                del bot.presence.joined_rooms[room_jid]

            bot.presence.broadcast()

            log.info("[ROOMS] 🚶 Left room %s", room_jid)

        log.info("[ROOMS] 🗑️ Deleted room %s", room_jid)

        bot.reply(
            msg,
            f"🗑️ Room removed: {room_jid}",
        )

    except Exception:
        log.exception("[ROOMS] 🗑️ Failed to delete room %s", room_jid)

        bot.reply(
            msg,
            f"🗑️ Failed remove room: {room_jid}",
        )


# -------------------------------------------------
# ROOMS LIST
# -------------------------------------------------

@command("rooms list", role=Role.ADMIN, aliases=["room list"])
async def rooms_list(bot, sender_jid, nick, args, msg, is_room):
    """
    Show all rooms stored in the database, if they are autojoin or not.

    Command
    -------
    {prefix}rooms list
    """

    rows = await bot.db.rooms.list()

    if not rows:
        bot.reply(msg, "ℹ️ No rooms stored.")
        return

    header = f"{'ROOM':40} {'NICK':15} {'AUTOJOIN':8} {'JOINED':6} {'STATUS'}"
    lines = ["📋 Stored rooms", header, "-" * len(header)]

    for room_jid, nick_name, autojoin, status in rows:

        autojoin_flag = "yes" if autojoin else "no"
        joined_flag = "yes" if room_jid in JOINED_ROOMS else "no"

        lines.append(
            f"{room_jid:40} {nick_name:15} {autojoin_flag:8} {joined_flag:6}"
            f" {status}"
        )

    header = (f"{'ROOM':40} {'NICK':15} {'AFFILIATION':10} {'ROLE':10}"
              f" {'AUTOJOIN':8} {'STATUS'}")
    lines += ["", "📋 JOINED rooms", header, "-" * len(header)]

    # Make defensive copy to avoid race conditions
    joined_rooms_copy = dict(JOINED_ROOMS)
    for room, data in joined_rooms_copy.items():
        try:
            nick = data.get("nick", "unknown")
            affiliation = data.get("affiliation", "unknown")
            role = data.get("role", "unknown")
            autojoin = data.get("autojoin", False)
            status = data.get("status") or ""
            autojoin_flag = "yes" if autojoin else "no"

            # Only display status if not empty and not empty JSON
            status_display = status if status and status != "{}" else ""

            lines.append(f"{room:40} {nick:15} {affiliation:10} {role:10}"
                         f" {autojoin_flag:8} {status_display}")
        except Exception as e:
            log.debug(f"[ROOMS] Error formatting room info for {room}: {e}")

    output = "\n".join(lines)
    bot.reply(msg, f"{output}")


# -------------------------------------------------
# ROOMS JOIN
# -------------------------------------------------

@command("rooms join", role=Role.ADMIN, aliases=["room join"])
async def rooms_join(bot, sender_jid, nick, args, msg, is_room):
    """
    Join a room immediately, add it to JOINED ROOMS and DB.

    Command
    -------
    {prefix}rooms join <room_jid> [nick]
    """

    if len(args) < 1 or len(args) > 2:
        bot.reply(
            msg,
            f"🟡️ Usage: {bot.prefix}rooms join <room_jid> [nick]",
        )
        return

    room_jid = args[0]

    if not await is_valid_room_jid(bot, room_jid, msg):
        bot.reply(
            msg,
            f"🟡️ Invalid room JID: {room_jid}",
        )
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not valid!")
        return

    if len(args) == 2:
        room_nick = args[1]
    else:
        room = await bot.db.rooms.get(room_jid)
        room_nick = room[1] if room else bot.boundjid.resource

    try:
        muc = bot.plugin["xep_0045"]

        await muc.join_muc(room_jid,
                           room_nick,
                           pshow=bot.presence.status["show"],
                           pstatus=bot.presence.status["status"])

        # Get current room state from DB
        db_room = await bot.db.rooms.get(room_jid)
        current_autojoin = db_room[2] if db_room else False
        current_status = db_room[3] if db_room else None

        if room_jid not in JOINED_ROOMS:
            JOINED_ROOMS[room_jid] = {
                "nick": room_nick,
                "autojoin": current_autojoin,
                "status": current_status,
                "affiliation": "unknown",
                "role": "unknown",
                "nicks": {}
            }

        bot.presence.joined_rooms[room_jid] = room_nick
        bot.presence.broadcast()

        # Only add if it doesn't exist; update if it does
        if db_room:
            # Room exists, only update nick if different
            if db_room[1] != room_nick:
                await bot.db.rooms.update(room_jid, nick=room_nick)
        else:
            # New room, add with autojoin=False (default for manual join)
            await bot.db.rooms.add(room_jid, room_nick, False)

        log.info("[ROOMS] 🚪 Joined room %s nick=%s", room_jid, room_nick)

        bot.reply(
            msg,
            f"🚪 Joined room: {room_jid}",
        )
    except Exception:
        log.exception("[ROOMS] 🚪 Joining room %s nick=%s FAILED!",
                      room_jid, room_nick)
        bot.reply(
            msg,
            f"🚪 Joining room FAILED: {room_jid}",
        )


# -------------------------------------------------
# ROOMS LEAVE
# -------------------------------------------------

@command("rooms leave", role=Role.ADMIN, aliases=["room leave"])
async def rooms_leave(bot, sender_jid, nick, args, msg, is_room):
    """
    Leave a joined room immediately. Doesn't touch the database. Only deletes
    it from the current JOINED_ROOMS list, without altering the 'autojoin'
    flag.

    Command
    -------
    {prefix}rooms leave <room_jid>
    """

    if len(args) != 1:
        bot.reply(
            msg,
            f"🟡️ Usage: {bot.prefix}rooms leave <room_jid>",
        )
        return

    room_jid = args[0]

    if not await is_valid_room_jid(bot, room_jid, msg):
        bot.reply(
            msg,
            f"🟡️ Invalid room JID: {room_jid}",
        )
        log.warning(f"[ROOMS] 🟡️ Room '{room_jid}' not valid!")
        return

    try:
        muc = bot.plugin["xep_0045"]

        room_data = JOINED_ROOMS.get(room_jid)
        if room_data:
            nick_to_leave = room_data.get("nick")
            if nick_to_leave:
                try:
                    muc.leave_muc(room_jid, nick_to_leave)
                except Exception as e:
                    log.warning(f"[ROOMS] Error leaving MUC: {e}")

        # --- Delete room completely from JOINED_ROOMS --
        JOINED_ROOMS.pop(room_jid, None)

        if room_jid in bot.presence.joined_rooms:
            del bot.presence.joined_rooms[room_jid]

        bot.presence.broadcast()

        log.info("[ROOMS] 🚶 Left room %s", room_jid)

        bot.reply(
            msg,
            f"🚶 Left room: {room_jid}",
        )

    except Exception:
        log.exception("[ROOMS] 🚶 Failed to leave room %s", room_jid)

        bot.reply(
            msg,
            f"🚶 Failed to leave room: {room_jid}",
        )


# -------------------------------------------------
# ROOMS SYNC
# -------------------------------------------------

@command("rooms sync", role=Role.ADMIN, aliases=["room sync"])
async def rooms_sync(bot, sender_jid, nick, args, msg, is_room):
    """
    Synchronize runtime rooms with database configuration. Leaves all rooms
    which have not set the 'autojoin' flag and joins the rooms which have the
    'autojoin' flag set.

    Command
    -------
    {prefix}rooms sync

    Description
    -----------
    Ensures that the bot's current room membership matches the
    configuration stored in the database.

    Actions performed
    -----------------
    • Leaves rooms joined by the bot but not stored in the database
    • Leaves all rooms which are in the database but haven't set the 'autojoin'
      flag.
    • Joins rooms that are configured with autojoin=true
    """
    try:
        rows = await bot.db.rooms.list()
    except Exception:
        log.exception("[ROOMS] 🔄 Failed to get rooms from DB")
        bot.reply(
            msg,
            "🔄 Failed to get rooms from DB",
        )
        return

    muc = bot.plugin["xep_0045"]
    left = []
    joined = []

    # Leave all currently joined rooms
    for room in list(JOINED_ROOMS.keys()):
        try:
            muc.leave_muc(room, JOINED_ROOMS[room]["nick"])
        except KeyError:
            log.debug(f"[ROOMS] rooms sync - Room already left: '{room}'")
        if room in bot.presence.joined_rooms:
            del bot.presence.joined_rooms[room]
        left.append(room)
    JOINED_ROOMS.clear()

    # Join only rooms from DB with autojoin=True
    for room_jid, nick_name, autojoin, status in rows:
        if autojoin:
            try:
                await muc.join_muc(
                    room_jid,
                    nick_name,
                    pshow=bot.presence.status['show'],
                    pstatus=bot.presence.status['status']
                )
                JOINED_ROOMS[room_jid] = {
                    "nick": nick_name,
                    "autojoin": autojoin,
                    "status": status,
                    "affiliation": "unknown",
                    "role": "unknown",
                    "nicks": {}
                }
                bot.presence.joined_rooms[room_jid] = nick_name
                joined.append(room_jid)
            except Exception:
                log.exception(f"[ROOMS] 🚪 Failed to join room {room_jid}")

    bot.presence.broadcast()

    log.info("[ROOMS] 🔄 Synchronization complete: joined=%d left=%d",
             len(joined), len(left))

    lines = ["🔄 Room synchronization complete"]
    if left:
        lines.append(f"🚶 Left: {', '.join(left)}")
    if joined:
        lines.append(f"🚪 Joined: {', '.join(joined)}")
    if not joined and not left:
        lines.append("ℹ️ No changes required.")

    bot.reply(
        msg,
        "\n".join(lines),
    )
