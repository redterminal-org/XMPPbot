"""
Bot presence and status management.

To turn on|off|status for this plugin, use the following command:
    {prefix}presence on|off|status

This plugin allows administrators to change the bot's XMPP presence
(online, away, do-not-disturb, etc.) and lets users view the current
presence state and status message.

Usage:
    {prefix}presence - Shows the bots current presence (status and message)
    {prefix}presence set <show> [message] - Sets the bot's presence

Available <show> statuses for setting the status:
    online - Normal online status
    chat - If you're available for chatting or searching for for talking.
    away - Away status indicator for 'Away From Keyboard' events.
    xa - 'Extended Away': For longer periods of being away, like sleeping.
    dnd - 'Do Not Disturb': If you don't want to be contacted at the moment
"""
import logging
from utils.command import command, Role
from plugins._core import (
    handle_room_toggle_command,
    _is_muc_pm,
    _get_enabled_rooms
)

log = logging.getLogger(__name__)

PRESENCE_KEY = "PRESENCE"
PLUGIN_META = {
    "name": "presence",
    "version": "0.2.1",
    "description": "Bot presence and status management",
    "category": "info",
}


@command("presence")
async def presence_show(bot, sender_jid, nick, args, msg, is_room):
    """
    Display the current bot presence and status message.

    To use on|off|status for this plugin, use the following command:
        {prefix}presence on|off|status

    Command
    -------
    {prefix}presence

    Description
    -----------
    Shows the bot's current presence state. The following states are supported:
    - online
        Normal online status
    - chat
        If you're available for chatting or searching for for talking.
    - away
        Away status indicator for 'Away From Keyboard' events.
    - xa
        'Extended Away': For longer periods of being away, like sleeping.
    - dnd
        'Do Not Disturb': If you don't want to be contacted at the moment

    And the optional status message that is broadcast to contacts
    and chatrooms.

    Example
    -------
    {prefix}presence
    """
    if is_room or _is_muc_pm(msg):
        handled = await handle_room_toggle_command(
            bot,
            msg,
            is_room,
            args,
            store_getter=get_presence_store,
            key=PRESENCE_KEY,
            label="Get/Set bot presence",
            storage="dict",
            log_prefix="[PRESENCE]",
        )
        if handled:
            return

    enabled_rooms = await _get_enabled_rooms(bot, PRESENCE_KEY, "presence")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ presence lookup is disabled in this room.")
        return

    show = bot.presence.status["show"]
    message = bot.presence.status["status"]

    emoji = bot.presence.emoji(show)

    if message:
        text = f"Current status {emoji} ({show}) {message}"
    else:
        text = f"Current status {emoji} ({show})"

    bot.reply(
        msg,
        text,
    )


@command("presence set", role=Role.ADMIN)
async def presence_set(bot, sender_jid, nick, args, msg, is_room):
    """
    Change the bot presence and optional status message.

    Command
    -------
    {prefix}presence set <show> [message]

    Description
    -----------
    Updates the presence state broadcast by the bot. Admins
    can use this command to indicate availability or activity.

    Valid Presence States (<show>)
    ---------------------
    online
    -   Default available presence.
    chat
    -   Actively available for conversation.
    away
    -   Temporarily away from the keyboard.
    xa
    -   Extended away.
    dnd
    -   Do not disturb.

    Parameters
    ----------
    show
    -   The presence state to set.
    message (optional)
    -   Additional human-readable status text.

    Examples
    --------
    {prefix}presence set away
    {prefix}presence set away Out for lunch
    {prefix}presence set dnd Busy working
    """
    enabled_rooms = await _get_enabled_rooms(bot, PRESENCE_KEY, "presence")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ presence setting is disabled in this room.")
        return

    if len(args) < 1:
        bot.reply(
            msg,
            f"Usage: {bot.prefix}status set <show> [message]",
        )
        return

    show = args[0].lower()
    message = " ".join(args[1:]) if len(args) > 1 else ""

    valid_states = {"online", "chat", "away", "xa", "dnd"}

    if show not in valid_states:
        bot.reply(
            msg,
            "Invalid status. Valid values: online, chat, away, xa, dnd",
        )
        return

    bot.presence.update(show, message)

    emoji = bot.presence.emoji(show)

    if message:
        response = f"Status updated {emoji} ({show}) {message}"
    else:
        response = f"Status updated {emoji} ({show})"

    bot.reply(
        msg,
        response,
    )

    log.info(f"[STATUS] {response}")


async def get_presence_store(bot):
    return bot.db.users.plugin("presence")
