"""SED plugin for message correction.

Allows users to correct previous messages using sed-like syntax.

IMPORTANT: You must enable SED corrections in a room for this to work.
Use this command to turn it on/off or show its status:
    {prefix}sed <on|off|status>

Commands:
• s/pattern/replacement/flags
• s#pattern#replacement#flags
• {prefix}sed <pattern> <replacement> [flags]
• {prefix}sed on/off/status

Flags:
• i - case insensitive
• m - multiline
• s - dotall
• g - global replace
• l - literal mode
"""

import logging
import multiprocessing
import queue
import re
import shlex
from functools import partial

from utils.command import command, Role
from utils.config import config
from plugins.rooms import JOINED_ROOMS
from plugins import _core

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "sed",
    "version": "0.5.0",
    "description": "Message correction using sed-like syntax",
    "category": "tools",
    "requires": ["rooms", "_core"],
}

SED_KEY = "SED"
CACHE_NAMESPACE = "sed"

# Hard timeout for regex substitution.
REGEX_TIMEOUT = 1.0

# Practical limits to reduce abuse.
MAX_PATTERN_LENGTH = 256
MAX_REPLACEMENT_LENGTH = 1000
MAX_INPUT_LENGTH = 5000
MAX_OUTPUT_LENGTH = 8000


def get_last_message(room: str):
    """Get the last message from cache."""
    entry = _core.get_last_cached_message(CACHE_NAMESPACE, room)
    if not entry:
        return None
    return entry.get("body")


def get_message_by_id(room: str, msg_id: str):
    """Get a message by stanza_id from cache."""
    entry = _core.get_cached_message_by_id(CACHE_NAMESPACE, room, msg_id)
    if not entry:
        return None
    return entry.get("body")


def _room_key_from_msg(msg, is_room: bool) -> str:
    if is_room:
        return msg["from"].bare

    room_full = str(msg["from"])
    return room_full.split("/", 1)[0] if "/" in room_full else room_full


# ============================================================================
# SED PARSING
# ============================================================================

def read_until_delimiter(raw_statement: str, delimiter: str, require: bool = True):
    """Read until an unescaped delimiter is found."""
    value = ""

    while True:
        try:
            sep_index = raw_statement.index(delimiter)
        except ValueError:
            if require:
                raise ValueError(f"Delimiter '{delimiter}' not found")

            return raw_statement, value

        if sep_index == 0:
            return value, raw_statement[1:]

        if raw_statement[sep_index - 1] == "\\":
            value += raw_statement[:sep_index - 1] + delimiter
            raw_statement = raw_statement[sep_index + 1:]
        else:
            value += raw_statement[:sep_index]
            raw_statement = raw_statement[sep_index + 1:]
            return value, raw_statement


def parse_sed_command(text: str):
    """Parse s/pattern/replacement/flags or s#pattern#replacement#flags.

    Returns:
        (pattern, replacement, flags) or (None, None, None)
    """
    if not text.startswith("s"):
        return None, None, None

    if len(text) < 2:
        return None, None, None

    delimiter = text[1]

    if delimiter not in ("/", "#"):
        return None, None, None

    try:
        raw_statement = text[2:]
        pattern, raw_statement = read_until_delimiter(raw_statement, delimiter)
        replacement, flags_str = read_until_delimiter(
            raw_statement,
            delimiter,
            require=False,
        )
        return pattern, replacement, flags_str

    except ValueError:
        return None, None, None


def _command_prefix() -> str:
    return config.get("prefix", ",")


def parse_prefixed_sed_command(text: str):
    """Parse '{prefix}sed <pattern> <replacement> [flags]' with shell-like quoting.

    Examples:
        ,sed foo bar
        ,sed 'lat(.*)' ''
        ,sed '++' '--' l
        ,sed "\\+\\+" -- g
    """
    prefix = _command_prefix()
    prefixed = f"{prefix}sed "

    if not text.startswith(prefixed):
        return None, None, None

    rest = text[len(prefixed):].strip()

    if not rest:
        return None, None, None

    try:
        parts = shlex.split(rest)
    except ValueError:
        return None, None, None

    if not parts:
        return None, None, None

    cmd = parts[0].lower()

    if cmd in {"on", "off", "status"} and len(parts) == 1:
        return None, None, None

    if len(parts) < 2:
        return None, None, None

    pattern = parts[0]
    replacement = parts[1]
    flags_str = "".join(parts[2:]) if len(parts) > 2 else ""

    return pattern, replacement, flags_str


def parse_any_sed_command(body: str):
    """Parse either inline sed syntax or prefixed sed syntax.

    Ignores leading reply quote lines.
    """
    lines = body.strip().split("\n")

    for line in lines:
        if line.startswith(">"):
            continue

        stripped = line.strip()

        if not stripped:
            continue

        pattern, replacement, flags_str = parse_sed_command(stripped)

        if pattern is not None:
            return pattern, replacement, flags_str

        pattern, replacement, flags_str = parse_prefixed_sed_command(stripped)

        if pattern is not None:
            return pattern, replacement, flags_str

        return None, None, None

    return None, None, None


def is_sed_command(body: str) -> bool:
    """Check if a message is a sed command, ignoring reply quotes."""
    pattern, replacement, flags_str = parse_any_sed_command(body)
    return pattern is not None


# ============================================================================
# REGEX APPLICATION
# ============================================================================

def _regex_worker(result_queue, original_text, pattern, replacement, flags_str):
    """Run regex substitution in a child process.

    This gives us a real timeout: the parent can terminate the process.
    """
    try:
        re_flags = 0
        global_replace = False
        literal_mode = False

        for flag in flags_str.lower():
            if flag == "i":
                re_flags |= re.IGNORECASE
            elif flag == "m":
                re_flags |= re.MULTILINE
            elif flag == "s":
                re_flags |= re.DOTALL
            elif flag == "g":
                global_replace = True
            elif flag == "l":
                literal_mode = True

        if literal_mode:
            pattern = re.escape(pattern)

        count = 0 if global_replace else 1
        new_text, num_replacements = re.subn(
            pattern,
            replacement,
            original_text,
            count=count,
            flags=re_flags,
        )

        result_queue.put(("ok", new_text, num_replacements))

    except re.error as exc:
        result_queue.put(("regex_error", str(exc), 0))

    except Exception as exc:
        result_queue.put(("error", str(exc), 0))


def apply_sed(original_text: str, pattern: str, replacement: str, flags_str: str):
    """Apply sed substitution with hard timeout protection.

    Returns:
        (new_text, num_replacements)
        (None, -1) on timeout
        (None, 0) on regex/validation error
    """
    try:
        if len(original_text) > MAX_INPUT_LENGTH:
            original_text = original_text[:MAX_INPUT_LENGTH]

        if len(pattern) > MAX_PATTERN_LENGTH:
            return None, 0

        if len(replacement) > MAX_REPLACEMENT_LENGTH:
            return None, 0

        valid_flags = {"i", "m", "s", "g", "l"}

        for flag in flags_str.lower():
            if flag not in valid_flags:
                return None, 0

        ctx = multiprocessing.get_context("fork")
        result_queue = ctx.Queue(maxsize=1)

        process = ctx.Process(
            target=_regex_worker,
            args=(
                result_queue,
                original_text,
                pattern,
                replacement,
                flags_str,
            ),
        )

        process.start()
        process.join(REGEX_TIMEOUT)

        if process.is_alive():
            process.terminate()
            process.join(0.2)

            if process.is_alive():
                process.kill()
                process.join(0.2)

            log.warning("[SED] Regex timeout - possible ReDoS pattern=%r", pattern)
            return None, -1

        try:
            status, value, num_replacements = result_queue.get_nowait()
        except queue.Empty:
            return None, 0

        if status == "ok":
            if len(value) > MAX_OUTPUT_LENGTH:
                value = value[:MAX_OUTPUT_LENGTH] + "…"
            return value, num_replacements

        if status == "regex_error":
            log.debug("[SED] Regex error for pattern=%r: %s", pattern, value)
            return None, 0

        log.warning("[SED] Regex worker error for pattern=%r: %s", pattern, value)
        return None, 0

    except Exception as exc:
        log.exception("[SED] Unexpected error in apply_sed: %s", exc)
        return None, 0


# ============================================================================
# BOT INTEGRATION
# ============================================================================

async def get_sed_store(bot):
    """Get the database store for sed settings."""
    return bot.db.users.plugin("sed")


def _is_direct_dm(msg, is_room: bool) -> bool:
    """Return True for normal 1:1 DMs, but not MUC PMs."""
    return (not is_room) and (msg["from"].bare not in JOINED_ROOMS)


def _sed_reply(bot, msg, text: str, is_room: bool):
    """Reply from sed.

    Disable thread in normal DMs to avoid duplicate rendering.
    """
    bot.reply(
        msg,
        text,
        mention=False,
        thread=not _is_direct_dm(msg, is_room),
    )


async def process_sed_correction(
    bot,
    nick,
    msg,
    is_room: bool,
    pattern: str,
    replacement: str,
    flags_str: str,
):
    """Process a sed correction."""
    room = _room_key_from_msg(msg, is_room)
    body = msg.get("body", "").strip()
    last_msg = None

    if body.startswith(">"):
        quoted_msg = _core.extract_reply_quote(body)

        if quoted_msg:
            last_msg = quoted_msg

    if not last_msg and is_room:
        reply_target_id = _core.get_reply_target(msg)

        if reply_target_id:
            last_msg = get_message_by_id(room, reply_target_id)

    if not last_msg:
        last_msg = get_last_message(room)

    if not last_msg:
        _sed_reply(bot, msg, "❌ No previous message found to correct.", is_room)
        return

    new_msg, num_replacements = apply_sed(
        last_msg,
        pattern,
        replacement,
        flags_str,
    )

    if num_replacements == -1:
        _sed_reply(
            bot,
            msg,
            "⏱️ Regex timeout - pattern took too long to process.",
            is_room,
        )
        return

    if new_msg is None:
        _sed_reply(
            bot,
            msg,
            f"❌ Regex error or invalid sed expression. Check your pattern: {pattern}",
            is_room,
        )
        return

    if num_replacements == 0:
        _sed_reply(
            bot,
            msg,
            f"❌ Pattern '{pattern}' not found in last message.",
            is_room,
        )
        return

    if is_room:
        response = f"> {last_msg}\n\n{new_msg}"
    else:
        response = new_msg

    _sed_reply(bot, msg, response, is_room)


@command("sed", role=Role.USER)
async def cmd_sed_handler(bot, sender_jid, nick, args, msg, is_room):
    """Handle sed corrections or enable/disable sed in a room."""
    if await _core.handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_sed_store,
        key=SED_KEY,
        label="SED corrections",
        storage="dict",
        log_prefix="[SED]",
    ):
        return

    prefix = _command_prefix()
    pattern, replacement, flags_str = parse_prefixed_sed_command(
        msg.get("body", "").strip()
    )

    if pattern is None:
        bot.reply(
            msg,
            f"❌ Usage: {prefix}sed <pattern> <replacement> [flags]",
        )
        return

    await process_sed_correction(
        bot,
        msg.get("mucnick"),
        msg,
        is_room,
        pattern,
        replacement,
        flags_str,
    )


async def on_message(bot, msg):
    """Handle sed commands and cache normal messages."""
    try:
        body = msg.get("body", "").strip()

        if not body:
            return

        if msg.get("from") == bot.boundjid:
            return

        stanza_id = _core.get_stanza_id(msg)

        if not _core.remember_stanza(CACHE_NAMESPACE, stanza_id):
            return

        is_room = msg.get("type") == "groupchat"
        nick = msg.get("mucnick") if is_room else None
        room = _room_key_from_msg(msg, is_room)

        if is_room:
            store = await get_sed_store(bot)
            enabled_rooms = await store.get_global(SED_KEY, default={})

            if not isinstance(enabled_rooms, dict):
                enabled_rooms = {}

            if enabled_rooms.get(room) is not True:
                return

            bot_nick = bot.presence.joined_rooms.get(room)

            if bot_nick and bot_nick == nick:
                return

        pattern, replacement, flags_str = parse_any_sed_command(body)

        if pattern is not None:
            await process_sed_correction(
                bot,
                nick,
                msg,
                is_room,
                pattern,
                replacement,
                flags_str,
            )
            return

        _core.cache_message(
            CACHE_NAMESPACE,
            room,
            nick,
            body,
            stanza_id,
            maxlen=10,
        )

    except Exception as exc:
        log.exception("[SED] Error in on_message: %s", exc)


async def on_load(bot):
    """Register the message event handlers."""
    bot.bot_plugins.register_event(
        "sed",
        "groupchat_message",
        partial(on_message, bot),
    )
    bot.bot_plugins.register_event(
        "sed",
        "message",
        partial(on_message, bot),
    )
