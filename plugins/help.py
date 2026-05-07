"""
📚 Help system for the bot.

This plugin provides dynamic help for:
• Plugins
• Commands
• Multi-word commands

IMPORTANT: You can turn on/off the in-room help with the command:
    {prefix}help inroom <on|off|status>

Usage
-----
General help:
  {prefix}help
Turn on help in rooms:
  {prefix}help inroom <on|off|status>
Plugin help:
  {prefix}help <plugin>
Command help:
  {prefix}help {prefix}<command>

Examples:
  {prefix}help rooms
  {prefix}help {prefix}timezone
  {prefix}help {prefix}presence set

Notes
-----
• Commands are filtered by user role.
• Plugins always display their full docstring.
• Command help displays the full command docstring.
"""

import logging
import slixmpp

from utils.command import (
    command,
    resolve_command,
    check_permission,
    Role,
    COMMANDS
)
from utils.config import config

from plugins._core import handle_room_toggle_command, _get_enabled_rooms

log = logging.getLogger(__name__)

HELP_KEY = "HELP"

PLUGIN_META = {
    "name": "help",
    "version": "0.3.0",
    "description": "Dynamic help for plugins and commands.",
    "category": "core",
    "requires": ["_core"],
}


# Store getter
async def get_help_store(bot):
    return bot.db.users.plugin("help")


# --------------------------------------------------
# DOCSTRING HELPERS
# --------------------------------------------------

def _first_line(doc):
    if not doc:
        return ""
    return doc.strip().splitlines()[0]


def _clean_doc(doc, prefix):
    if not doc:
        return ""

    lines = []

    for line in doc.strip().splitlines():
        lines.append(line.replace("{prefix}", prefix).rstrip())

    return "\n".join(lines)


# --------------------------------------------------
# QUERY EXTRACTION
# --------------------------------------------------

def _extract_query(msg, prefix):
    """
    Extract raw help query from message body.

    This avoids command token normalization so that
    multi-word commands like "status set" work correctly.
    """

    body = msg["body"].strip()

    if not body.startswith(prefix):
        return ""

    body = body[len(prefix):].strip()

    if not body.lower().startswith("help"):
        return ""

    return body[4:].strip()


# --------------------------------------------------
# COMMAND DISCOVERY
# --------------------------------------------------

def _commands_for_plugin(bot, plugin_name, user_role):
    """
    Dynamically collect commands belonging to a plugin.

    This reads the live command registry and removes duplicates
    caused by command aliases.
    """

    commands = []
    seen = set()

    tokens_list = COMMANDS.by_plugin.get(plugin_name, ())

    for tokens in tokens_list:
        cmd = COMMANDS.get(tokens)

        if not cmd:
            continue

        # skip aliases (same Command object)
        if id(cmd) in seen:
            continue

        if not check_permission(user_role, cmd):
            continue

        seen.add(id(cmd))
        commands.append(cmd)

    commands.sort(key=lambda c: c.name)

    return commands


# --------------------------------------------------
# COMMAND FORMATTER
# --------------------------------------------------

def _format_command(cmd_obj, prefix):
    """
    Format command entry for plugin help.
    """

    name = cmd_obj.name
    role = str(cmd_obj.role)

    desc = _first_line(cmd_obj.handler.__doc__) or ""

    aliases = cmd_obj.aliases or []

    # remove canonical name if it appears as alias
    aliases = [a for a in aliases if a != name]

    # ensure deterministic order
    aliases = sorted(set(aliases))

    if aliases:
        alias_text = " (" + ", ".join(prefix + a for a in aliases) + ")"
    else:
        alias_text = ""

    return f"{prefix}{name}{alias_text} [{role}] - {desc}"


# --------------------------------------------------
# HELP COMMAND
# --------------------------------------------------

@command("help", aliases=["h"])
async def cmd_help(bot, sender_jid, nick, args, msg, is_room):
    """
    Show help.

    Usage:
      {prefix}help
      {prefix}help <plugin>
      {prefix}help {prefix}<command>
    """

    prefix = config.get("prefix", ",")

    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _get_enabled_rooms(bot, HELP_KEY, "help")
    if is_room and msg["from"].bare not in enabled_rooms:
        bot.reply(msg, "ℹ️ Help is only available via private message in this room.")
        return

    query = " ".join(args).strip()

    room = msg['from'].bare
    nick = msg['from'].resource

    jid = None
    muc = bot.plugin.get("xep_0045", None)
    if muc:
        jid = slixmpp.JID(muc.get_jid_property(
            room, nick, "jid"))
    if jid == "":
        jid = sender_jid
    jid = str(slixmpp.JID(jid))

    # determine sender role
    user_role = await bot.get_user_role(jid, room)

    pm = bot.bot_plugins

    # --------------------------------------------------
    # GENERAL HELP
    # --------------------------------------------------

    if not query:

        lines = ["📦 Available plugins", ""]

        for name, module in sorted(pm.plugins.items()):

            # hide internal plugins for non-admin users
            if name.startswith("_") and user_role > Role.ADMIN:
                continue

            # collect commands visible to this user
            commands = _commands_for_plugin(bot, name, user_role)

            # hide plugins with no visible commands for non-admin users
            if user_role > Role.ADMIN and not commands:
                continue

            doc = _first_line(module.__doc__) or ""
            lines.append(f"• {name} — {doc}")

        lines.append("")
        lines.append(f"Use {prefix}help <plugin> for plugin help.")
        lines.append(f"Use {prefix}help {prefix}<command> for command help.")

        bot.reply(msg, lines)
        return

    # --------------------------------------------------
    # COMMAND HELP
    # --------------------------------------------------

    if query.startswith(prefix):

        cmd_text = query[len(prefix):].strip()

        cmd_obj, _ = resolve_command(cmd_text)

        if not cmd_obj:
            bot.reply(msg, "🟡️ Unknown command.")
            return

        if not check_permission(user_role, cmd_obj):
            bot.reply(msg, "⛔ You do not have permission to use this command.")
            return

        doc = _clean_doc(cmd_obj.handler.__doc__, prefix)

        lines = [
            f"📖 Command: {prefix}{cmd_obj.name}",
            ""
        ]

        if doc:
            lines.append(doc)

        bot.reply(msg, lines)
        return

    # --------------------------------------------------
    # PLUGIN HELP
    # --------------------------------------------------

    plugin = query.lower()

    # hide internal plugins for non-admin users
    if plugin.startswith("_") and user_role > Role.ADMIN:
        bot.reply(msg, "🟡️ Unknown plugin.")
        return

    if plugin not in pm.plugins:
        bot.reply(msg, "🟡️ Unknown plugin.")
        return

    module = pm.plugins[plugin]

    lines = [
        f"📦 Plugin: {plugin}",
        ""
    ]

    module_doc = _clean_doc(module.__doc__, prefix)

    if module_doc:
        lines.append(module_doc)
        lines.append("")

    lines.append("Commands:")

    commands = _commands_for_plugin(bot, plugin, user_role)

    if not commands:
        lines.append("No commands available for your role.")
    else:
        for cmd in commands:
            lines.append(_format_command(cmd, prefix))

    bot.reply(msg, lines)

@command("help inroom", role=Role.USER, aliases=["h inroom"])
async def help_inroom_command(bot, sender_jid, sender_nick, args, msg, is_room):
    """
    Toggles usage of help inside a particular chat room.
    This is stored on a per-room basis and does not affect private messages.

    Usage:
        {prefix}help inroom <on|off|status>
    """

    handled = await handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_help_store,
        key=HELP_KEY,
        label="In-Room Help",
        storage="dict",
        log_prefix="[HELP]",
    )
    if handled:
        return

    bot.reply(msg, f"Usage: {config.get('prefix', ',')}help inroom <on|off|status>")
    return
