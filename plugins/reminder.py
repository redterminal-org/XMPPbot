"""Schedule and manage reminders.

Schedule reminders to notify you at a later time.

Commands:
• {prefix}remind <duration|date time> <message> - Set a new reminder
• {prefix}reminders - List all your pending reminders
• {prefix}remind delete <id> - Delete a reminder by ID
• {prefix}remind <on|off|status> - Enable, disable, or show reminder status
• {prefix}reminder <on|off|status> - Same as above

Duration formats:
• Single: 10s, 5m, 1h, 2d
• Combined: 1h30m, 2d5h, 3d12h30m45s

Date/time formats:
• ISO-like: 2026-05-01 14:30, 2026-05-01T14:30
• German: 01.05.2026 14:30, 01.05.26 14:30

Absolute date/time values are interpreted in the user's vCard TIMEZONE if set.
If no valid vCard TIMEZONE exists, UTC is used.

Examples:
• {prefix}remind 30m Take a break
• {prefix}remind 1h Important meeting
• {prefix}remind 2d5h3m20s Long term goal with exact time
• {prefix}remind 2026-05-01 14:30 Birthday reminder
• {prefix}remind 01.05.2026 14:30 Birthday reminder
• {prefix}reminders
• {prefix}remind delete 1
• {prefix}remind <on|off|status>
• {prefix}reminder <on|off|status>

Limits:
• Maximum reminder duration/date distance: 365 days by default
• Maximum message length: 500 characters
"""

import asyncio
import datetime
import logging

import pytz

from utils.command import command, Role
from utils.config import config
from plugins._core import (
    handle_room_toggle_command,
    get_user_tzinfo,
    JOINED_ROOMS,
    _is_muc_pm,
    _normalize_bare_jid,
    parse_duration,
)

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "reminder",
    "version": "0.2.2",
    "description": "Schedule and manage reminders",
    "category": "utility",
    "requires": ["_core", "rooms"],
}

# In-memory storage of active asyncio tasks: {reminder_id: task}
ACTIVE_REMINDERS: dict[int, asyncio.Task] = {}

# Runtime switch for the reminder plugin. Defaults to enabled.
# Optional config.json override: "reminder_enabled": false
REMINDER_ENABLED: bool = bool(config.get("reminder_enabled", True))
REMINDER_KEY = "REMINDER"

# The plugin initializes its DB table lazily and on_ready().
REMINDER_DB_READY = False


# ============================================================================
# HELPERS
# ============================================================================


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_tz():
    return pytz.UTC


def _display_nick(sender_jid, nick: str | None = None) -> str:
    """Best-effort display name for reminder messages."""
    if nick:
        return str(nick)

    value = str(sender_jid)

    if "/" in value:
        resource = value.rsplit("/", 1)[-1]
        if resource:
            return resource

    if "@" in value:
        return value.split("@", 1)[0]

    return value


def _timezone_lookup_jid(bot, sender_jid, msg, is_room: bool) -> str | None:
    """Return the best real JID to use for vCard TIMEZONE lookup."""
    if not is_room and not _is_muc_pm(msg, is_room):
        try:
            return str(msg["from"].bare)
        except Exception:
            return _normalize_bare_jid(sender_jid)

    try:
        muc = getattr(bot, "plugin", {}).get("xep_0045", None)
        if muc:
            room = msg["from"].bare
            nick = msg["from"].resource
            real_jid = muc.get_jid_property(room, nick, "jid")
            if real_jid:
                return _normalize_bare_jid(real_jid)
    except Exception as exc:
        log.debug("[REMINDER] Could not resolve MUC real JID for timezone: %s",
                  exc)

    try:
        room = msg["from"].bare
        muc_nick = msg["from"].resource
        joined = JOINED_ROOMS.get(room, {})
        nick_info = joined.get("nicks", {}).get(muc_nick, {})
        real_jid = nick_info.get("jid")
        if real_jid:
            return _normalize_bare_jid(real_jid)
    except Exception as exc:
        log.debug("[REMINDER] Could not resolve JOINED_ROOMS JID for timezone: %s", exc)

    return _normalize_bare_jid(sender_jid)


def _localize_naive_datetime(
    dt: datetime.datetime,
    tz: datetime.tzinfo,
) -> datetime.datetime:
    """Attach timezone to a naive datetime, handling pytz timezones safely."""
    if dt.tzinfo is not None:
        return dt

    if hasattr(tz, "localize"):
        try:
            return tz.localize(dt, is_dst=None)
        except pytz.NonExistentTimeError:
            # DST spring-forward gap: move to the next valid local hour.
            return tz.localize(dt + datetime.timedelta(hours=1), is_dst=True)
        except pytz.AmbiguousTimeError:
            # DST fall-back duplicate hour: choose standard time.
            return tz.localize(dt, is_dst=False)

    return dt.replace(tzinfo=tz)


def _format_local_datetime(
    dt: datetime.datetime,
    tz: datetime.tzinfo,
) -> str:
    """Format a UTC datetime in the user's local timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _reminder_context(bot, sender_jid, nick, msg, is_room: bool):
    """Build stable ownership and delivery context.

    Cases:
    - normal DM: send chat to bare user JID
    - MUC: send groupchat to room bare JID
    - MUC-PM: send chat to full occupant JID room@conference/nick
    """
    if is_room:
        room_jid = msg["from"].bare
        user_jid = _normalize_bare_jid(sender_jid)
        display_nick = _display_nick(sender_jid, nick)

        return {
            "user_jid": user_jid,
            "timezone_jid": _timezone_lookup_jid(bot, sender_jid, msg, is_room),
            "display_nick": display_nick,
            "room_jid": room_jid,
            "msg_mto": room_jid,
            "msg_type": "groupchat",
        }

    if _is_muc_pm(msg, is_room):
        muc_occupant_jid = str(msg["from"])
        display_nick = msg["from"].resource or _display_nick(sender_jid, nick)

        return {
            "user_jid": muc_occupant_jid,
            "timezone_jid": _timezone_lookup_jid(bot, sender_jid, msg, is_room),
            "display_nick": display_nick,
            "room_jid": None,
            "msg_mto": muc_occupant_jid,
            "msg_type": "chat",
        }

    user_jid = _normalize_bare_jid(sender_jid)

    return {
        "user_jid": user_jid,
        "timezone_jid": user_jid,
        "display_nick": _display_nick(sender_jid, nick),
        "room_jid": None,
        "msg_mto": user_jid,
        "msg_type": "chat",
    }


async def get_reminder_store(bot):
    """Return the plugin runtime store used for room-scoped settings."""
    return bot.db.users.plugin("reminder")


def _room_jid_from_context(msg, is_room: bool) -> str | None:
    """Return the room JID for groupchat or MUC-PM contexts.

    The other room-controlled plugins use MUC-PM room management, where
    is_room is False but msg["from"].bare is the room JID. Public groupchat
    messages have is_room=True. Normal DMs return None.
    """
    try:
        room_jid = str(msg["from"].bare)
    except Exception:
        return None

    if is_room:
        return room_jid

    if room_jid in JOINED_ROOMS:
        return room_jid

    return None


async def _get_room_reminder_state(bot, room_jid: str) -> bool:
    """Return whether reminders are enabled for a room.

    This intentionally matches plugins/rooms.py dict semantics:
    {room_jid: True} means enabled. Missing keys are disabled, even if the
    configured default is on, because rooms.py writes defaults explicitly.
    """
    try:
        store = await get_reminder_store(bot)
        state = await store.get_global(REMINDER_KEY, default={})
    except Exception as exc:
        log.exception(
            "[REMINDER] Error reading room control state for %s: %s",
            room_jid,
            exc,
        )
        return False

    if not isinstance(state, dict):
        return False

    return bool(state.get(room_jid))


async def _is_reminder_enabled_for_context(bot, msg, is_room: bool) -> bool:
    """Return whether reminders may be used in the current context.

    Normal DMs are allowed. Groupchat and MUC-PM contexts must be enabled via
    the room control state.
    """
    room_jid = _room_jid_from_context(msg, is_room)
    if not room_jid:
        return True

    return await _get_room_reminder_state(bot, room_jid)


async def _handle_reminder_control_command(bot, args, msg, is_room: bool) -> bool:
    """Handle reminder on/off/status.

    Room contexts are delegated to 
    utils.plugin_helper.handle_room_toggle_command.  Normal DMs control
    the global runtime kill-switch.
    """
    global REMINDER_ENABLED

    if not args:
        return False

    subcmd = str(args[0]).lower()
    if subcmd not in {"on", "off", "status"}:
        return False

    room_jid = _room_jid_from_context(msg, is_room)

    if room_jid:
        before = await _get_room_reminder_state(bot, room_jid)

        handled = await handle_room_toggle_command(
            bot,
            msg,
            is_room,
            args,
            store_getter=get_reminder_store,
            key=REMINDER_KEY,
            label="Use 'reminder' commands",
            storage="dict",
            log_prefix="[REMINDER]",
        )

        if handled:
            after = await _get_room_reminder_state(bot, room_jid)

            if subcmd == "on" and not before and after and REMINDER_ENABLED:
                restored = await _restore_pending_reminders(bot)
                log.info(
                    "[REMINDER] Room %s enabled via helper; restored %s reminders",
                    room_jid,
                    restored,
                )

            elif subcmd == "off" and before and not after:
                cancelled = await _cancel_active_tasks_for_room(bot, room_jid)
                log.info(
                    "[REMINDER] Room %s disabled via helper; cancelled %s tasks",
                    room_jid,
                    cancelled,
                )

        return handled

    # Normal DM: global runtime switch.
    if subcmd == "status":
        global_state = "on" if REMINDER_ENABLED else "off"
        active_count = sum(1 for task in ACTIVE_REMINDERS.values() if not task.done())
        bot.reply(
            msg,
            f"ℹ️ Reminder plugin global: {global_state}. "
            f"Active scheduled reminders: {active_count}.",
        )
        return True

    if subcmd == "on":
        if REMINDER_ENABLED:
            bot.reply(msg, "ℹ️ Reminder plugin is already globally on.")
            return True

        REMINDER_ENABLED = True
        restored = await _restore_pending_reminders(bot)
        bot.reply(
            msg,
            f"▶️ Reminder plugin enabled globally. "
            f"Restored {restored} pending reminder task(s).",
        )
        log.info("[REMINDER] Plugin enabled globally; restored %s reminders",
                 restored)
        return True

    if not REMINDER_ENABLED:
        bot.reply(msg, "ℹ️ Reminder plugin is already globally off.")
        return True

    REMINDER_ENABLED = False
    cancelled = await _cancel_all_active_tasks()
    bot.reply(
        msg,
        f"⏸️ Reminder plugin disabled globally. Pending reminders stay saved. "
        f"Cancelled {cancelled} active task(s).",
    )
    log.info("[REMINDER] Plugin disabled globally; cancelled %s tasks",
             cancelled)
    return True


def format_seconds(total_seconds: float) -> str:
    """Convert seconds to a human-readable duration."""
    if total_seconds < 0:
        return "overdue"

    days = int(total_seconds // 86400)
    remaining = total_seconds % 86400

    hours = int(remaining // 3600)
    remaining %= 3600

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    parts = []

    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def _ensure_utc(
    dt: datetime.datetime,
    assume_tz: datetime.tzinfo | None = None,
) -> datetime.datetime:
    """Return timezone-aware UTC datetime.

    Naive datetime values are interpreted in assume_tz. If no timezone is
    supplied, UTC is used as fallback.
    """
    if dt.tzinfo is None:
        dt = _localize_naive_datetime(dt, assume_tz or _utc_tz())

    return dt.astimezone(datetime.timezone.utc)


def parse_absolute_datetime(
    args: list[str],
    user_tz: datetime.tzinfo | None = None,
) -> tuple[datetime.datetime | None, int]:
    """Parse an absolute date/time from the beginning of command arguments.

    Returns (datetime_utc, consumed_arg_count), or (None, 0) if parsing fails.
    """
    if not args:
        return None, 0

    candidates: list[tuple[str, int]] = [(args[0], 1)]

    if len(args) >= 2:
        candidates.append((" ".join(args[:2]), 2))

    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%y %H:%M",
        "%d.%m.%y %H:%M:%S",
    ]

    for candidate, consumed in candidates:
        for fmt in formats:
            try:
                dt = datetime.datetime.strptime(candidate, fmt)
                return _ensure_utc(dt, user_tz), consumed
            except ValueError:
                continue

    return None, 0


def parse_reminder_when(
    args: list[str],
    user_tz: datetime.tzinfo | None = None,
) -> tuple[int | None, str | None, str | None]:
    """Parse relative duration or absolute date/time from reminder args.

    Returns (seconds_until_reminder, message, display_when). If parsing fails,
    returns (None, None, None).
    """
    if len(args) < 2:
        return None, None, None

    seconds = parse_duration(args[0])
    if seconds is not None:
        message = " ".join(args[1:]).strip()
        if not message:
            return None, None, None

        return seconds, message, f"in {format_seconds(seconds)}"

    remind_at, consumed = parse_absolute_datetime(args, user_tz)
    if remind_at is None or len(args) <= consumed:
        return None, None, None

    message = " ".join(args[consumed:]).strip()
    if not message:
        return None, None, None

    seconds = int((remind_at - _utcnow()).total_seconds())
    if seconds < 1:
        return None, None, None

    display_when = f"on {_format_local_datetime(remind_at, user_tz or _utc_tz())}"
    return seconds, message, display_when


def _format_overdue(seconds: float) -> str:
    overdue_seconds = abs(seconds)

    if overdue_seconds < 60:
        return f"{int(overdue_seconds)}s ago"
    if overdue_seconds < 3600:
        return f"{int(overdue_seconds / 60)}m ago"
    if overdue_seconds < 86400:
        return f"{overdue_seconds / 3600:.1f}h ago"

    return f"{overdue_seconds / 86400:.1f}d ago"


def _parse_datetime(value) -> datetime.datetime:
    """Handle DB values returned as datetime or ISO string."""
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        dt = datetime.datetime.fromisoformat(str(value))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    return dt


# ============================================================================
# SELF-CONTAINED DATABASE HELPERS
# ============================================================================


async def _init_reminder_db(bot):
    """Create the reminders table and indexes if they do not exist.

    Keeping this inside the plugin makes reminder.py self-contained: the core
    database manager only has to provide execute()/fetch_all().
    """
    global REMINDER_DB_READY

    if REMINDER_DB_READY:
        return

    await bot.db.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY,
            user_jid TEXT NOT NULL,
            room_jid TEXT,
            message TEXT NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            remind_at TIMESTAMP NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await bot.db.execute("""
        CREATE INDEX IF NOT EXISTS idx_reminders_user_jid
        ON reminders(user_jid)
    """)

    await bot.db.execute("""
        CREATE INDEX IF NOT EXISTS idx_reminders_remind_at
        ON reminders(remind_at)
    """)

    await bot.db.execute("""
        CREATE INDEX IF NOT EXISTS idx_reminders_is_active
        ON reminders(is_active)
    """)

    REMINDER_DB_READY = True
    log.info("[REMINDER] ✅ Initialized reminders table")


async def _create_reminder(
    bot,
    user_jid: str,
    message: str,
    scheduled_at: datetime.datetime,
    remind_at: datetime.datetime,
    room_jid: str | None = None,
) -> int:
    """Insert a reminder and return its ID."""
    await _init_reminder_db(bot)

    cursor = await bot.db.execute(
        """
        INSERT INTO reminders
        (user_jid, room_jid, message, scheduled_at, remind_at, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (
            user_jid,
            room_jid,
            message,
            scheduled_at.isoformat(),
            remind_at.isoformat(),
        ),
    )

    return cursor.lastrowid


async def _get_reminder(bot, reminder_id: int) -> dict | None:
    """Return one reminder by ID, or None if it does not exist."""
    await _init_reminder_db(bot)

    rows = await bot.db.fetch_all(
        "SELECT * FROM reminders WHERE id = ?",
        (reminder_id,),
    )

    if not rows:
        return None

    return dict(rows[0])


async def _get_pending_reminders(bot, user_jid: str) -> list[dict]:
    """Return pending reminders for one user ordered by due date."""
    await _init_reminder_db(bot)

    rows = await bot.db.fetch_all(
        """
        SELECT * FROM reminders
        WHERE user_jid = ? AND is_active = 1
        ORDER BY remind_at ASC
        """,
        (user_jid,),
    )

    return [dict(row) for row in rows]


async def _get_all_pending_reminders(bot) -> list[dict]:
    """Return all pending reminders ordered by due date."""
    await _init_reminder_db(bot)

    rows = await bot.db.fetch_all(
        """
        SELECT * FROM reminders
        WHERE is_active = 1
        ORDER BY remind_at ASC
        """
    )

    return [dict(row) for row in rows]


async def _delete_reminder(bot, reminder_id: int):
    """Delete one reminder by ID."""
    await _init_reminder_db(bot)

    await bot.db.execute(
        "DELETE FROM reminders WHERE id = ?",
        (reminder_id,),
    )


# ============================================================================
# DELIVERY / SCHEDULING
# ============================================================================


async def _send_reminder_message(bot, mto: str, mbody: str, mtype: str):
    """Send reminder as a fresh message.

    Do not use bot.reply() here because delayed reminders should not depend on
    an old message object or client-specific reply/thread rendering.
    """
    msg = bot.make_message(
        mto=mto,
        mbody=mbody,
        mtype=mtype,
    )

    if hasattr(bot, "_safe_send_message"):
        await bot._safe_send_message(msg)
    else:
        msg.send()


async def schedule_reminder_task(
    bot,
    reminder_id: int,
    user_jid: str,
    nick: str,
    message: str,
    seconds: float,
    original_msg,
    overdue_str: str | None = None,
    room_jid: str | None = None,
    msg_mto: str | None = None,
    msg_type: str | None = None,
):
    """Background task that waits and sends the reminder.

    Works for both new reminders and restored reminders after bot restart.
    """
    try:
        await asyncio.sleep(max(0.1, float(seconds)))

        if not REMINDER_ENABLED:
            log.info(
                "[REMINDER] Reminder %s due while plugin disabled; keeping pending",
                reminder_id,
            )
            return

        if room_jid and not await _get_room_reminder_state(bot, room_jid):
            log.info(
                "[REMINDER] Reminder %s due while room %s disabled; keeping pending",
                reminder_id,
                room_jid,
            )
            return

        if room_jid:
            if overdue_str:
                reminder_text = f"🔔 {nick}: Reminder (was due {overdue_str}): {message}"
            else:
                reminder_text = f"🔔 {nick}: Reminder: {message}"
        else:
            if overdue_str:
                reminder_text = f"🔔 Reminder (was due {overdue_str}): {message}"
            else:
                reminder_text = f"🔔 Reminder: {message}"

        try:
            target = msg_mto or (room_jid if room_jid else user_jid)
            message_type = msg_type or ("groupchat" if room_jid else "chat")

            await _send_reminder_message(
                bot,
                mto=target,
                mbody=reminder_text,
                mtype=message_type,
            )

            log.info(
                "[REMINDER] ✅ Reminder %s sent to %s",
                reminder_id,
                target,
            )

        except Exception as exc:
            log.exception(
                "[REMINDER] Failed to send reminder %s: %s",
                reminder_id,
                exc,
            )
            return

        await _delete_reminder(bot, reminder_id)
        log.info("[REMINDER] ✅ Reminder %s deleted after sending", reminder_id)

    except asyncio.CancelledError:
        log.debug("[REMINDER] ⚠️ Reminder %s was cancelled", reminder_id)
        raise

    except Exception as exc:
        log.exception("[REMINDER] Error in reminder task %s: %s", reminder_id, exc)

    finally:
        ACTIVE_REMINDERS.pop(reminder_id, None)


def _schedule_task(
    bot,
    reminder_id: int,
    user_jid: str,
    nick: str,
    message: str,
    seconds: float,
    original_msg,
    overdue_str: str | None = None,
    room_jid: str | None = None,
    msg_mto: str | None = None,
    msg_type: str | None = None,
):
    """Create or replace an active reminder task safely."""
    old_task = ACTIVE_REMINDERS.get(reminder_id)

    if old_task and not old_task.done():
        old_task.cancel()

    task = asyncio.create_task(
        schedule_reminder_task(
            bot,
            reminder_id,
            user_jid,
            nick,
            message,
            seconds,
            original_msg,
            overdue_str=overdue_str,
            room_jid=room_jid,
            msg_mto=msg_mto,
            msg_type=msg_type,
        )
    )

    ACTIVE_REMINDERS[reminder_id] = task
    return task


async def _cancel_all_active_tasks() -> int:
    """Cancel all active in-memory reminder tasks and return the count."""
    cancelled = 0

    for reminder_id, task in list(ACTIVE_REMINDERS.items()):
        if task and not task.done():
            task.cancel()
            cancelled += 1

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception(
                "[REMINDER] Error cancelling reminder %s: %s",
                reminder_id,
                exc,
            )

    ACTIVE_REMINDERS.clear()
    return cancelled


async def _cancel_active_tasks_for_room(bot, room_jid: str) -> int:
    """Cancel active in-memory reminder tasks belonging to one room."""
    pending = await _get_all_pending_reminders(bot)
    room_reminder_ids = {
        int(reminder["id"])
        for reminder in pending
        if reminder.get("room_jid") == room_jid
    }

    cancelled = 0

    for reminder_id in room_reminder_ids:
        task = ACTIVE_REMINDERS.pop(reminder_id, None)

        if task and not task.done():
            task.cancel()
            cancelled += 1

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception(
                "[REMINDER] Error cancelling room reminder %s: %s",
                reminder_id,
                exc,
            )

    return cancelled


async def _restore_pending_reminders(bot) -> int:
    """Restore pending reminders from the database.

    Returns the number of reminders scheduled in memory.
    """
    pending = await _get_all_pending_reminders(bot)

    if not pending:
        log.info("[REMINDER] ✅ No pending reminders to restore")
        return 0

    restored = 0
    now = _utcnow()

    for reminder in pending:
        reminder_id = reminder["id"]
        user_jid = reminder["user_jid"]
        room_jid = reminder.get("room_jid")
        message = reminder["message"]
        remind_at = _parse_datetime(reminder["remind_at"])

        existing_task = ACTIVE_REMINDERS.get(reminder_id)
        if existing_task and not existing_task.done():
            log.debug("[REMINDER] Reminder %s already scheduled; skipping",
                      reminder_id)
            continue

        if room_jid and not await _get_room_reminder_state(bot, room_jid):
            log.debug(
                "[REMINDER] Reminder %s belongs to disabled room %s; skipping restore",
                reminder_id,
                room_jid,
            )
            continue

        time_left = remind_at - now
        seconds_left = time_left.total_seconds()
        overdue_str = None

        if seconds_left < 0.1:
            overdue_str = _format_overdue(seconds_left)
            log.info(
                "[REMINDER] ⏰ Reminder %s is overdue (%s), sending now",
                reminder_id,
                overdue_str,
            )
            seconds_left = 0.1

        display_nick = _display_nick(user_jid)

        # Backwards-compatible delivery restore:
        # - room_jid set: old/new MUC reminder -> send groupchat to room
        # - otherwise normal DM or MUC-PM -> send chat to stored user_jid
        if room_jid:
            msg_mto = room_jid
            msg_type = "groupchat"
        else:
            msg_mto = user_jid
            msg_type = "chat"

        try:
            _schedule_task(
                bot,
                reminder_id,
                user_jid,
                display_nick,
                message,
                seconds_left,
                None,
                overdue_str=overdue_str,
                room_jid=room_jid,
                msg_mto=msg_mto,
                msg_type=msg_type,
            )

            restored += 1
            hours = seconds_left / 3600

            log.info(
                "[REMINDER] ✅ Restored reminder %s: %s (%.1f h remaining)",
                reminder_id,
                message,
                hours,
            )

        except Exception as exc:
            log.exception(
                "[REMINDER] Error restoring reminder %s: %s",
                reminder_id,
                exc,
            )

    if restored > 0:
        log.info("[REMINDER] ✅ Successfully restored %s pending reminders",
                 restored)

    return restored


# ============================================================================
# COMMANDS
# ============================================================================


@command("remind", role=Role.USER, aliases=["rem", "reminder"])
async def remind_command(bot, sender_jid, nick, args, msg, is_room):
    """Set a new reminder."""
    prefix = config.get("prefix", ",")

    if await _handle_reminder_control_command(bot, args, msg, is_room):
        return

    if not REMINDER_ENABLED:
        bot.reply(
            msg,
            f"⏸️ Reminder plugin is globally off. Use {prefix}remind on in a DM to enable it.",
        )
        return

    if not await _is_reminder_enabled_for_context(bot, msg, is_room):
        bot.reply(
            msg,
            f"⏸️ Reminders are disabled for this room. Use {prefix}reminder on in a MUC DM to enable them here.",
        )
        return

    if len(args) < 2:
        bot.reply(
            msg,
            f"ℹ️ Usage: {prefix}remind <duration|date time> <message>\n"
            f"Example: {prefix}remind 30m Take a break\n"
            f"Example: {prefix}remind 2026-05-01 14:30 Take a break\n"
            f"Example: {prefix}remind 01.05.2026 14:30 Take a break\n"
            "Formats: 10s, 5m, 1h, 2d, 1h30m, "
            "YYYY-MM-DD HH:MM, DD.MM.YYYY HH:MM "
            f"(max {config.get('reminder_max_age_days', 365)} days)",
        )
        return

    try:
        ctx = _reminder_context(bot, sender_jid, nick, msg, is_room)
        user_tz = await get_user_tzinfo(bot, ctx.get("timezone_jid"))

        seconds, message, display_when = parse_reminder_when(args, user_tz)

        if seconds is None or seconds < 1 or not message:
            bot.reply(
                msg,
                "❌ Invalid reminder time.\n"
                "Use relative format: 10s, 5m, 1h, 2d, 1h30m\n"
                "Or absolute format: 2026-05-01 14:30, 01.05.2026 14:30",
            )
            return

        max_days = config.get("reminder_max_age_days", 365)
        max_seconds = max_days * 24 * 3600

        if seconds > max_seconds:
            bot.reply(msg, f"❌ Reminder too far in the future. Maximum is {max_days} days.")
            return

        if len(message) > 500:
            bot.reply(msg, "❌ Message too long. Maximum is 500 characters.")
            return

        user_jid = ctx["user_jid"]
        display_nick = ctx["display_nick"]
        room_jid = ctx["room_jid"]
        msg_mto = ctx["msg_mto"]
        msg_type = ctx["msg_type"]

        scheduled_at = _utcnow()
        remind_at = scheduled_at + datetime.timedelta(seconds=seconds)

        reminder_id = await _create_reminder(
            bot,
            user_jid=user_jid,
            message=message,
            scheduled_at=scheduled_at,
            remind_at=remind_at,
            room_jid=room_jid,
        )

        _schedule_task(
            bot,
            reminder_id,
            user_jid,
            display_nick,
            message,
            seconds,
            msg,
            room_jid=room_jid,
            msg_mto=msg_mto,
            msg_type=msg_type,
        )

        bot.reply(msg, f"✅ Reminder set! I'll remind you {display_when}")
        log.info("[REMINDER] Created reminder %s for %s: %s", reminder_id,
                 user_jid, message)

    except Exception as exc:
        log.exception("[REMINDER] Error creating reminder: %s", exc)
        bot.reply(msg, "❌ Error creating reminder. Please try again.")


@command("reminders", role=Role.USER, aliases=["rems", "remind list"])
async def list_reminders(bot, sender_jid, nick, args, msg, is_room):
    """List all pending reminders for the current user."""
    try:
        ctx = _reminder_context(bot, sender_jid, nick, msg, is_room)
        user_jid = ctx["user_jid"]
        user_tz = await get_user_tzinfo(bot, ctx.get("timezone_jid"))

        reminders = await _get_pending_reminders(bot, user_jid)

        if not reminders:
            bot.reply(msg, "✅ No pending reminders.")
            return

        lines = ["⏰ Your pending reminders:"]

        for reminder in reminders:
            remind_at = _parse_datetime(reminder["remind_at"])
            time_left = remind_at - _utcnow()
            time_str = format_seconds(time_left.total_seconds())
            local_time = _format_local_datetime(remind_at, user_tz)

            lines.append(
                f"• ID {reminder['id']}: {reminder['message']} "
                f"(in {time_str}, at {local_time})"
            )

        bot.reply(msg, "\n".join(lines))

    except Exception as exc:
        log.exception("[REMINDER] Error listing reminders: %s", exc)
        bot.reply(msg, "❌ Error retrieving reminders.")


@command("remind delete", role=Role.USER,
         aliases=["remind rm", "remind cancel"])
async def delete_reminder(bot, sender_jid, nick, args, msg, is_room):
    """Delete or cancel a reminder by ID."""
    prefix = config.get("prefix", ",")

    if not args:
        bot.reply(msg, f"ℹ️ Usage: {prefix}remind delete <id>")
        return

    try:
        reminder_id = int(args[0])
    except ValueError:
        bot.reply(msg, "❌ Reminder ID must be a number.")
        return

    try:
        ctx = _reminder_context(bot, sender_jid, nick, msg, is_room)
        user_jid = ctx["user_jid"]

        reminder = await _get_reminder(bot, reminder_id)

        if not reminder:
            bot.reply(msg, "❌ Reminder not found.")
            return

        if reminder["user_jid"] != user_jid:
            bot.reply(msg, "❌ You can only delete your own reminders.")
            return

        await _delete_reminder(bot, reminder_id)

        task = ACTIVE_REMINDERS.pop(reminder_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        bot.reply(msg, f"✅ Reminder {reminder_id} deleted.")
        log.info("[REMINDER] Deleted reminder %s", reminder_id)

    except Exception as exc:
        log.exception("[REMINDER] Error deleting reminder: %s", exc)
        bot.reply(msg, "❌ Error deleting reminder.")


# ============================================================================
# PLUGIN LIFECYCLE
# ============================================================================

async def on_ready(bot):
    """
    Initialize the reminder table and restore pending reminders after
    startup/reload.
    """
    try:
        await _init_reminder_db(bot)

        if not REMINDER_ENABLED:
            log.info("[REMINDER] Plugin is disabled; pending reminders will not be restored")
            return

        log.info("[REMINDER] Loading pending reminders from database...")
        await _restore_pending_reminders(bot)

    except Exception as exc:
        log.exception("[REMINDER] Error during reminder restoration: %s", exc)


async def on_unload(bot):
    """Cancel all active reminder tasks."""
    try:
        log.info("[REMINDER] Unloading reminder plugin...")

        cancelled = await _cancel_all_active_tasks()
        log.info("[REMINDER] ✅ Plugin unloaded; cancelled %s task(s)",
                 cancelled)

    except Exception as exc:
        log.exception("[REMINDER] Error during plugin unload: %s", exc)
