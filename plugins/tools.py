"""
Tools plugin: Utility commands for bot interaction including ping/pong, message echo,
timezone-aware time/date lookups, and Unix timestamp conversion.

To use on/off/status to turn on/off or show the status of the plugin, use:
    {prefix}tools on|off|status

Provides basic bot health checks, message echoing, and allows users to query the current
time and date in their configured timezone or another user's timezone, as well as convert
Unix timestamps.

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
from datetime import datetime
from utils.command import command, Role
from utils.config import config
from plugins.rooms import JOINED_ROOMS
from plugins._core import (
    _is_muc_pm,
    handle_room_toggle_command,
    _get_enabled_rooms,
    _get_user_timezone,
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
        bot.reply(msg, f"Usage: {config.get('prefix', ',')}tools on|off|status")
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

    bot.reply(msg, "Usage: {prefix}information on|off|status (in a room or PM)")


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
    Show the current time in your configured timezone or another user's timezone.

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
                bot.reply(msg, f"🔴  Nick '{target_nick}' not found in this room.")
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
                       f"Set with {config.get('prefix', ',')}tz set <timezone>")
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
    bot.reply(msg, f"⏰ Time for {display_name}: {formatted} {tzone}{loc_str}", ephemeral=False)


@command("date", role=Role.USER)
async def date_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the current date in your configured timezone or another user's timezone.

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
                bot.reply(msg, f"🔴  Nick '{target_nick}' not found in this room.")
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
                       f"Set with {config.get('prefix', ',')}tz set <timezone>")
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
    bot.reply(msg, f"📅 Date for {display_name}: {formatted} ({tzone}){loc_str}", ephemeral=False)


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
        bot.reply(msg, f"🔴 Usage: {config.get('prefix', ',')}ts <unix_timestamp>")
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

        bot.reply(msg, f"⏰ Timestamp {timestamp} = {formatted} ({tzone})", ephemeral=False)
    except (ValueError, OSError):
        bot.reply(msg, f"🔴 Invalid timestamp or out of range.")
    except Exception as e:
        bot.reply(msg, f"🔴 Error converting timestamp: {str(e)}")
