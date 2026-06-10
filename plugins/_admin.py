"""
Admin management commands.

This plugin exposes administrative commands for bot management,
like restart, shutdown, and status monitoring.

The bot restart command simply stops the bot, which is then restarted by
the system's service functions.

The bot shutdown command is built from the "stop_cmd" key in the config.json
like this:
"stop_cmd": ["/usr/bin/systemctl", "--user", "stop", "envsbot.service"]
(This may be different for your setup and used init system)
"""

import logging
import asyncio
import os
import json
import subprocess
import psutil
from datetime import datetime
from utils.command import command, Role
from utils.config import config
from plugins._core import (
    JOINED_ROOMS,
)

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "admin",
    "version": "0.1.2",
    "description": "Bot administration commands",
    "category": "core",
    "requires": ["_core"],
}

# Use a temp file to store restart notification data
RESTART_NOTIFICATION_FILE = "/tmp/bot_restart_notification.json"

# Track bot start time
BOT_START_TIME = None


def set_bot_start_time(bot):
    """Initialize bot start time tracking."""
    global BOT_START_TIME
    if BOT_START_TIME is None:
        BOT_START_TIME = datetime.now()


def human_time(seconds: int) -> str:
    """Convert seconds to human-readable string."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"

    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)

    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")

    return " ".join(parts)


def human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable size string."""
    if size_bytes < 0:
        return "unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size_bytes} B"


# ------------------------------------------------
# Helper functions to reduce cyclomatic complexity
# ------------------------------------------------
async def _get_db_status(bot):
    lines = []
    # Database status
    db_status = "✅ Connected" if getattr(
        bot, "db", None) else "❌ Disconnected"
    lines.append(f"Database: {db_status}")

    # Database size
    try:
        db_path = getattr(bot.db, "path", None)
        if db_path and os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            lines.append(f"Database Size: {human_size(db_size)}")
        elif db_path:
            lines.append(f"Database Size: file not found ({db_path})")
    except Exception as e:
        log.debug("[ADMIN] Could not get database size: %s", e)
    return lines


async def _get_plugin_status(bot):
    lines = []
    # Loaded plugins
    loaded_plugins = len(bot.bot_plugins.plugins)
    available_plugins = len(list(bot.bot_plugins.discover()))
    lines.append(f"Plugins: {loaded_plugins}/{available_plugins} loaded")
    lines.append("")
    return lines


async def _get_bot_uptime(bot):
    # Time the bot is running
    if BOT_START_TIME:
        bot_uptime = datetime.now() - BOT_START_TIME
        bot_uptime_str = human_time(bot_uptime.total_seconds())
        return [f"Bot Uptime: {bot_uptime_str}"]


async def _get_connection_time(bot):
    # Server/XMPP connection uptime
    try:
        connection_start = getattr(bot, "connection_start_time", None)
        if connection_start:
            connection_uptime = datetime.now() - connection_start
            connection_uptime_str = human_time(
                connection_uptime.total_seconds()
            )
            return [f"Server Connection: {connection_uptime_str}"]
    except Exception as e:
        log.debug("[ADMIN] Could not get connection uptime: %s", e)
        return ["Server Connection: unknown"]


async def _get_memory_usage(bot):
    # Memory usage
    try:
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / 1024 / 1024
        return [f"Memory Usage: {memory_mb:.1f} MB"]
    except Exception as e:
        log.debug("[ADMIN] Could not get memory info: %s", e)
        return ["Memory Usage: unknown"]


async def _get_cpu_usage(bot):
    lines = []
    # CPU usage
    try:
        process = psutil.Process(os.getpid())
        loop = asyncio.get_event_loop()

        cpu_percent = await loop.run_in_executor(
            None,
            process.cpu_percent,
            1.0
        )

        cpu_load = psutil.getloadavg()[0]
        cpu_count = psutil.cpu_count()

        lines.append(f"CPU Usage: {cpu_percent:.1f}% (Process)")
        lines.append(f"System Load: {cpu_load:.2f} ({cpu_count} cores)")
        lines.append("")
    except Exception as e:
        log.debug("[ADMIN] Could not get CPU info: %s", e)
        lines.append("CPU usage/system load: unknown")
        lines.append("")
    return lines


async def _get_joined_rooms(bot):
    lines = []
    # Connected rooms (from rooms plugin)
    try:
        joined_rooms = len(JOINED_ROOMS)
        lines.append(f"Connected Rooms: {joined_rooms}")
        if joined_rooms > 0:
            for room, room_data in sorted(JOINED_ROOMS.items()):
                room_nick = room_data.get("nick", "unknown")
                lines.append(f"  • {room} (nick: {room_nick})")
    except Exception as e:
        log.debug("[ADMIN] Could not get rooms info: %s", e)
        lines.append("Connected Rooms: unknown")
    return lines


# --------------
# ADMIN COMMANDS
# --------------
@command("bot restart", role=Role.OWNER, aliases=["restart"])
async def bot_restart(bot, sender, nick, args, msg, is_room):
    """
    Restart the entire bot process.

    Gracefully disconnects, closes the database, and restarts the bot
    using the system's service functionality.

    Usage:
        {prefix}bot restart
    """
    bot.reply(msg, "🔄 Bot restarting...")
    log.info("[ADMIN] 🔄 Bot restart requested by %s", sender)

    # Wait a moment to ensure the reply is sent
    await asyncio.sleep(0.5)

    # Initiate graceful shutdown
    log.info("[ADMIN] Initiating graceful shutdown...")
    bot.disconnect()

    # Store restart notification info to file
    notification_data = {
        "sender": str(sender),
        "sender_bare":
            str(sender.bare) if hasattr(sender, "bare") else str(sender),
        "nick": nick,
        "room":
            str(msg["from"].bare) if msg.get("type") == "groupchat" else None,
        "is_room": is_room,
    }

    try:
        with open(RESTART_NOTIFICATION_FILE, "w") as f:
            json.dump(notification_data, f)
        log.info(
            "[ADMIN] Restart notification saved to %s",
            RESTART_NOTIFICATION_FILE
        )
    except Exception as e:
        log.error("[ADMIN] Failed to save restart notification: %s", e)

    # Wait for disconnect with timeout
    try:
        await asyncio.wait_for(bot.disconnected, timeout=5)
    except asyncio.TimeoutError:
        log.warning(
            "[ADMIN] Disconnect timeout - proceeding with restart anyway")

    # Close database
    try:
        await bot.db.close()
    except Exception as e:
        log.error("[ADMIN] Error closing database: %s", e)


@command("bot shutdown", role=Role.OWNER, aliases=["shutdown"])
async def bot_shutdown(bot, sender, nick, args, msg, is_room):
    """
    Gracefully shutdown the bot.

    Shuts down the bot using the "stop_cmd" list from the config file
    which builds the shutdown system command, for example:
    ["/usr/bin/systemctl", "--user", "stop", "envsbot.service"]
    (This may be different for your setup and used system init system)

    Usage:
        {prefix}bot shutdown
    """
    try:
        stop_cmd = config["stop_cmd"]
        if not stop_cmd:
            raise KeyError
    except KeyError:
        bot.reply(msg, "🛑 No shutdown command provided in config file!")
        log.info("[ADMIN] 🛑 Shutdown request failed. No shutdown command"
                 " in config file provided.")
        return
    bot.reply(msg, "🛑 Bot shutting down...")
    log.info("[ADMIN] 🛑 Bot shutdown requested by %s", sender)

    subprocess.run(stop_cmd)


@command("bot status", role=Role.ADMIN, aliases=["bot info"])
async def bot_status(bot, sender, nick, args, msg, is_room):
    """
    Display current bot status and statistics.

    Shows uptime, connected users, loaded plugins, memory usage,
    and database info.

    Usage:
        {prefix}bot status
        {prefix}bot info
    """
    # The functions to get the information was separated to reduce cyclomatic
    # complexity
    try:
        set_bot_start_time(bot)

        lines = ["🤖 Bot Status"]
        lines.append("")

        # JID info
        lines.append(f"JID: {bot.boundjid}")
        lines.append("")

        # get database status
        lines.extend(await _get_db_status(bot))
        # get plugin status
        lines.extend(await _get_plugin_status(bot))
        # get bot uptime
        lines.extend(await _get_bot_uptime(bot))
        # get server connection time
        lines.extend(await _get_connection_time(bot))
        # get memory usage
        lines.extend(await _get_memory_usage(bot))
        # get CPU usage
        lines.extend(await _get_cpu_usage(bot))
        # get joined rooms
        lines.extend(await _get_joined_rooms(bot))

        bot.reply(msg, lines)

    except Exception as e:
        log.error("[ADMIN] Error getting bot status: %s", e)
        bot.reply(msg, "❌ Failed to retrieve bot status")


async def on_load(bot):
    """Initialize admin plugin."""
    set_bot_start_time(bot)
    log.info("[ADMIN] Admin plugin loaded")
