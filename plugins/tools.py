"""
Tools plugin: Utility commands for bot interaction including ping/pong,
message echo, timezone-aware time/date lookups, and Unix timestamp conversion.

To use on/off/status to turn on/off or show the status of the plugin, use:
    {prefix}tools on|off|status

Provides basic bot health checks, message echoing, and allows users to
query the current time and date in their configured timezone or another
user's timezone, as well as convert Unix timestamps.

Commands:
    {prefix}ping
    {prefix}echo <message>
    {prefix}time [nick]
    {prefix}date [nick]
    {prefix}utc
    {prefix}ts <unix_timestamp>
"""

import pytz
import logging
import slixmpp
from datetime import datetime
from datetime import timezone as dt_timezone
from utils.command import command, Role
from utils.config import config
from plugins.rooms import JOINED_ROOMS
from plugins._core import (
    _is_muc_pm,
    handle_room_toggle_command,
    _get_enabled_rooms,
    _get_user_timezone,
    get_user_tzinfo,
    get_jids_from_nick_index,
)

log = logging.getLogger(__name__)

TOOLS_KEY = "TOOLS"
PLUGIN_META = {
    "name": "tools",
    "version": "0.4.0",
    "description": "Utility commands: ping/pong, message echo, timezone-aware time/date lookups, and Unix timestamp conversion",
    "category": "utility",
    "requires": ["_core", "vcard"],
}


@command("tools", role=Role.MODERATOR)
async def information_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Toggle tools plugin features in the current room.

    Usage:
        {prefix}tools on|off|status
    """
    if not args:
        bot.reply(msg,
                  f"Usage: {config.get('prefix', ',')}tools on|off|status")
        return

    if is_room or _is_muc_pm(msg):
        handled = await handle_room_toggle_command(
            bot,
            msg,
            is_room,
            args,
            store_getter=get_tools_store,
            key=TOOLS_KEY,
            label="Get online infos",
            storage="dict",
            log_prefix="[INFORMATION]",
        )
        if handled:
            return

    bot.reply(msg,
              "Usage: {prefix}information on|off|status (in a room or PM)")


async def get_tools_store(bot):
    return bot.db.users.plugin("tools")


@command("ping", role=Role.USER, aliases=["pong"])
async def ping_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Respond with a pong message to confirm the bot is alive.

    Usage:
        {prefix}ping
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ ping is disabled in this room.")
        return

    bot.reply(msg, "🏓 Pong!", ephemeral=False)


@command("echo", role=Role.USER)
async def echo_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Repeat a message back to the user.

    Usage:
        {prefix}echo <message>

    Examples:
        {prefix}echo Hello World!
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ echo is disabled in this room.")
        return

    if not args:
        bot.reply(msg, f"🔴 Usage: {config.get('prefix', ',')}echo <message>")
        return

    # Join all arguments to handle multi-word messages
    message = " ".join(args)

    # Escape any special characters for safety if needed
    bot.reply(msg, f"🔊 {message}", ephemeral=False)


@command("time", role=Role.USER, aliases=["t"])
async def time_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the current time in your configured timezone or another user's
    timezone.

    Usage:
        {prefix}time
        {prefix}time <nick>
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ time is disabled in this room.")
        return

    room = msg["from"].bare
    nicks = JOINED_ROOMS.get(room, {}).get("nicks", {})
    if is_room or _is_muc_pm(msg):
        if args:
            target_nick = " ".join(args).strip()
            info = nicks.get(target_nick)
            if not info or not info.get("jid"):
                bot.reply(msg,
                          f"🔴  Nick '{target_nick}' not found in this room.")
                return
            target_jid = str(info["jid"])
            display_name = target_nick
        else:
            info = nicks.get(nick)
            if not info or not info.get("jid"):
                bot.reply(msg,
                          "🔴  Could not determine your JID in this room.")
                return
            target_jid = str(info["jid"])
            display_name = nick
    else:
        # Direct messages to bot are vorbidden
        if args:
            log.info(f"[VCARD] Direct message with args from '{msg['from'].bare}'")
            bot.reply(msg, "🔴  In direct messages, you can only look up your own vCard. Use the command without args.")
            return
        target_jid = str(msg["from"].bare)
        display_name = target_jid

    timezone = await _get_user_timezone(bot, target_jid)

    if not timezone:
        bot.reply(msg, f"🟡️ No TIMEZONE set for {display_name}. Using UTC. "
                       f"Set with {config.get('prefix', ',')}tz set"
                       " <timezone>")
        tzinfo = pytz.UTC
        tzone = "UTC"
    else:
        try:
            tzinfo = pytz.timezone(timezone)
            tzone = timezone
        except Exception:
            bot.reply(msg, f"🟡️ Invalid timezone '{timezone}' for {display_name}. Using UTC.")
            tzinfo = pytz.UTC
            tzone = "UTC"

    now = datetime.now(tzinfo)
    formatted = now.strftime("%Y-%m-%d %H:%M:%S")
    loc_str = ""
    bot.reply(msg, f"⏰ Time for {display_name}: {formatted} {tzone}{loc_str}",
              ephemeral=False)


@command("date", role=Role.USER)
async def date_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the current date in your configured timezone or another user's
    timezone.

    Usage:
        {prefix}date
        {prefix}date <nick>
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ date is disabled in this room.")
        return

    room = msg["from"].bare
    nicks = JOINED_ROOMS.get(room, {}).get("nicks", {})
    if is_room or _is_muc_pm(msg):
        if args:
            target_nick = " ".join(args).strip()
            info = nicks.get(target_nick)
            if not info or not info.get("jid"):
                bot.reply(msg,
                          f"🔴  Nick '{target_nick}' not found in this room.")
                return
            target_jid = str(info["jid"])
            display_name = target_nick
        else:
            info = nicks.get(nick)
            if not info or not info.get("jid"):
                bot.reply(msg, "🔴  Could not determine your JID in this room.")
                return
            target_jid = str(info["jid"])
            display_name = nick
    else:
        # Direct messages are not allowed
        if args:
            log.info(f"[VCARD] Direct message with args from '{msg['from'].bare}'")
            bot.reply(msg, "🔴  In direct messages, you can only look up your own vCard. Use the command without args.")
            return
        target_jid = str(msg["from"].bare)
        display_name = target_jid

    timezone = await _get_user_timezone(bot, target_jid)

    if not timezone:
        bot.reply(msg, f"🟡️ No TIMEZONE set for {display_name}. Using UTC. "
                       f"Set with {config.get('prefix', ',')}tz set"
                       " <timezone>")
        tzinfo = pytz.UTC
        tzone = "UTC"
    else:
        try:
            tzinfo = pytz.timezone(timezone)
            tzone = timezone
        except Exception:
            bot.reply(msg, f"🟡️ Invalid timezone '{timezone}' for {display_name}. Using UTC.")
            tzinfo = pytz.UTC
            tzone = "UTC"

    now = datetime.now(tzinfo)
    formatted = now.strftime("%Y-%m-%d")
    loc_str = ""
    bot.reply(msg,
              f"📅 Date for {display_name}: {formatted} ({tzone}){loc_str}",
              ephemeral=False)


@command("utc", role=Role.USER)
async def utc_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the current UTC time as a quick reference.

    Usage:
        {prefix}utc
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ utc is disabled in this room.")
        return

    now = datetime.now(pytz.UTC)
    formatted = now.strftime("%Y-%m-%d %H:%M:%S")
    bot.reply(msg, f"🌍 Current UTC time: {formatted}", ephemeral=False)


@command("ts", role=Role.USER)
async def timestamp_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Convert a Unix timestamp to human-readable date and time in your timezone.

    Usage:
        {prefix}ts <unix_timestamp>

    Examples:
        {prefix}ts 1704067200
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ ts is disabled in this room.")
        return

    if not args:
        bot.reply(msg,
                  f"🔴 Usage: {config.get('prefix', ',')}ts <unix_timestamp>")
        return

    try:
        timestamp = int(args[0])
    except ValueError:
        bot.reply(msg, f"🔴 Invalid timestamp. Please provide a valid Unix timestamp (integer).")
        return

    try:
        # Get user's timezone
        target_jid = JOINED_ROOMS.get(msg["from"].bare, {}).get("nicks", {}).get(nick, {}).get("jid", str(msg["from"].bare))
        timezone = await _get_user_timezone(bot, target_jid)

        if timezone:
            try:
                tzinfo = pytz.timezone(timezone)
            except Exception:
                tzinfo = pytz.UTC
        else:
            tzinfo = pytz.UTC

        # Convert timestamp to datetime in user's timezone
        dt = datetime.fromtimestamp(timestamp, tz=pytz.UTC)
        dt_local = dt.astimezone(tzinfo)
        formatted = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        tzone = str(tzinfo) if timezone else "UTC"

        bot.reply(msg, f"⏰ Timestamp {timestamp} = {formatted} ({tzone})",
                  ephemeral=False)
    except (ValueError, OSError):
        bot.reply(msg, "🔴 Invalid timestamp or out of range.")
    except Exception as e:
        bot.reply(msg, f"🔴 Error converting timestamp: {str(e)}")


@command(name="seen", role=Role.USER, aliases=["s"])
async def seen_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the last seen time and current presence for a given nickname.
    Works in groupchats, groupchat PMs, and DMs.
    Uses the caller's timezone and always displays the provided nickname.
    """
    enabled_rooms = await _get_enabled_rooms(bot, TOOLS_KEY, "tools")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ seen is disabled in this room.")
        return

    try:
        joined_rooms = JOINED_ROOMS
        _nick_index = getattr(bot, "_nick_index", {})    # {nick: [jid1, jid2,...]}
        msg_from = msg['from']
        from_str = str(msg_from)
        has_resource = '/' in from_str
        bare_jid = from_str.split('/')[0]

        if bare_jid in joined_rooms:
            # MUC or MUC PM context
            room_jid = bare_jid
            joined_room = joined_rooms[room_jid]
            nicks_dict = joined_room.get("nicks", {})
            caller_nick = from_str.split('/', 1)[1] if has_resource else nick
            display_nick = " ".join(args).strip() if args else caller_nick

            # 1. Try JOINED_ROOMS first (active occupant in this room)
            present_in_room = False
            target_jid = None
            candidate_info = nicks_dict.get(display_nick)
            if candidate_info and "jid" in candidate_info:
                target_jid = candidate_info["jid"]
                present_in_room = True
            else:
                # 2. Fallback: historical JID from _nick_index
                candidates = get_jids_from_nick_index(bot, display_nick)
                if candidates:
                    target_jid = candidates[0]
                else:
                    target_jid = None
                present_in_room = False

            # Get caller realjid (for timezone)
            caller_info = nicks_dict.get(caller_nick)
            if caller_info and "jid" in caller_info:
                caller_jid = caller_info["jid"]
            else:
                caller_jid = _nick_index.get(caller_nick, [None])[0]

            if not target_jid:
                log.info(f"[SEEN] Nick not found in MUC or index: '{display_nick}' (room={room_jid}) requested by '{caller_nick}'")
                bot.reply(msg, f"🔴 Nick '{display_nick}' not found in this room or index.")
                return
            if not caller_jid:
                log.warning(f"[SEEN] Caller nick '{caller_nick}' not found in room index while requesting seen for '{display_nick}' in room {room_jid}.")
                bot.reply(msg, "🔴 Could not determine your JID in this room.")
                return

            # Presence: only if currently in the room
            target_show = "unknown"
            target_status = ""
            target_emoji = ""
            if present_in_room:
                try:
                    muc_plugin = bot.plugin['xep_0045']
                    occupants = muc_plugin.get_roster(room_jid)
                    if display_nick in occupants:
                        target_show = muc_plugin.get_jid_property(room_jid,
                                                                  display_nick,
                                                                  'show') or 'online'
                        target_status = muc_plugin.get_jid_property(room_jid,
                                                                  display_nick,
                                                                  'status') or ''
                        target_emoji = bot.presence.emoji(target_show)
                    else:
                        log.info(f"[SEEN] Occupant '{display_nick}' not found via get_occupant in {room_jid}")
                except Exception as e:
                    log.warning(f"[SEEN] Could not get presence for '{display_nick}' in room {room_jid}: {e}")
                    # fallback: unknown

        else:
            # Not a MUC context → regular DM (or fallback)
            if args and " ".join(args).strip() != nick:
                log.info(f"[SEEN] DM lookup denied: '{nick}' tried to look up '{args[0]}' in PM.")
                bot.reply(msg, "🔴 Can only look up yourself in private chats.")
                return
            display_nick = nick
            candidates = _nick_index.get(display_nick, [])
            target_jid = candidates[0] if candidates else slixmpp.JID(sender_jid).bare
            caller_jid = target_jid
            target_show = "online"
            target_status = ""
            target_emoji = bot.presence.emoji(target_show)

        # Get the user's timezone using their real JID
        try:
            timezone = await get_user_tzinfo(bot, caller_jid)
        except Exception as ex:
            log.warning(f"[SEEN] Failed to get timezone for {caller_jid}: {ex}")
            timezone = None

        # Retrieve last_seen from users table
        user = await bot.db.users.get(target_jid)
        if not user:
            log.info(f"[SEEN] No data found for nick '{display_nick}' (jid={target_jid})")
            bot.reply(msg, f"🔴 No data found for nick '{display_nick}'.")
            return

        last_seen_utc = user.get("last_seen")
        last_seen_str = "never"
        if last_seen_utc:
            try:
                dt_utc = datetime.fromisoformat(last_seen_utc)
                if timezone:
                    try:
                        if not dt_utc.tzinfo:
                            dt_utc = dt_utc.replace(tzinfo=dt_timezone.utc)
                        dt_local = dt_utc.astimezone(timezone)
                        last_seen_str = dt_local.strftime("%Y-%m-%d %H:%M:%S %Z")
                    except Exception as e:
                        log.warning(f"[SEEN] Failed to convert last_seen for '{display_nick}' to tz {timezone}: {e}")
                        last_seen_str = dt_utc.isoformat()
                else:
                    last_seen_str = dt_utc.isoformat()
            except Exception as e:
                log.warning(f"[SEEN] Malformed last_seen for '{display_nick}': {e}")
                last_seen_str = str(last_seen_utc)

        lines = [
            f"👤 Nickname: {display_nick}",
            f"🕒 Last seen: {last_seen_str}",
            f"-  Status: {target_emoji} {target_show} ({target_status})" if present_in_room and target_show != "unknown" else "Status: unknown",
        ]

        log.info(f"[SEEN] Lookup for '{display_nick}': seen='{last_seen_str}' status={target_show} jid={target_jid}")
        bot.reply(msg, "\n".join(lines))
    except Exception as exc:
        log.exception(f"[SEEN] Unexpected error in seen command for '{nick}': {exc}")
        bot.reply(msg, "🔴 Unexpected error in seen command.")
