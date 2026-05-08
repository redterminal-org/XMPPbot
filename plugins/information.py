"""
Info plugin.

This plugin provides various information commands:
- Wikipedia summary lookup
- Fetch latest toot from a Fediverse user
- Urban Dictionary term search
- Fetch an acronym's meaning (Limited to 100 requests per day, so use wisely!)

Commands:
    {prefix}wikipedia <search term>
    {prefix}fediverse <@user@instance>
    {prefix}udict <term>
    {prefix}acronyms <ACRONYM>
    {prefix}information on|off|status (to toggle in rooms)
"""

import aiohttp
import asyncio
import requests
import html
import logging
import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from utils.command import command, Role
from utils.config import config
from plugins._core import (
    handle_room_toggle_command,
    _is_muc_pm,
    _get_enabled_rooms
)

log = logging.getLogger(__name__)

INFO_KEY = "INFORMATION"

PLUGIN_META = {
    "name": "information",
    "version": "0.4.0",
    "description": "Wikipedia, Fediverse and Urban Dictionary lookup.",
    "category": "info",
    "requires": ["_core"],
}


# ---------------- Fediverse ----------------

FEDIVERSE_USER_RE = re.compile(r"^@?([^@]+)@([^@]+)$")


def html_to_text_with_links(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            a.replace_with(f"{a.get_text()} ({href})")
    text = soup.get_text(separator=" ", strip=True)
    return html.unescape(text)


@command("fediverse", role=Role.USER, aliases=["fedi"])
async def fediverse_latest(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the latest public toot from a Fediverse user.

    Usage:
        {prefix}fediverse <@user@instance>
        {prefix}fedi <@user@instance>

    Example:
        {prefix}fediverse @Gargron@mastodon.social
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Fediverse lookup is disabled in this room.")
        return

    if not args:
        bot.reply(
            msg,
            f"🟡️ Usage: {config.get('prefix', ',')}fediverse <@user@instance>"
        )
        return

    match = FEDIVERSE_USER_RE.match(args[0])
    if not match:
        log.warning("[FEDIVERSE] 🟡️ Invalid user format.")
        bot.reply(
            msg,
            "🟡️ Please specify the user as @user@instance"
        )
        return

    username, instance = match.groups()
    url = f"https://{instance}/api/v1/accounts/lookup?acct={username}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    log.warning("[FEDIVERSE] 🔴  User not found on instance.")
                    bot.reply(msg, "🔴  User not found on this instance.")
                    return
                user = await resp.json()
            user_id = user.get("id")
            if not user_id:
                log.warning("[FEDIVERSE] 🔴  Could not resolve user ID.")
                bot.reply(msg, "🔴  Could not resolve user.")
                return
            timeline_url = (
                f"https://{instance}/api/v1/accounts/{user_id}/statuses"
                "?limit=1&exclude_replies=false&exclude_reblogs=false"
            )
            async with session.get(timeline_url, timeout=8) as resp:
                if resp.status != 200:
                    log.warning("[FEDIVERSE] 🔴  Could not fetch user timeline.")
                    bot.reply(msg, "🔴  Could not fetch user timeline.")
                    return
                statuses = await resp.json()
    except Exception:
        log.exception("[FEDIVERSE] 🚨 Error fetching from Fediverse.")
        bot.reply(msg, "🔴  Error fetching from Fediverse.")
        return

    if not statuses:
        bot.reply(msg, "ℹ️ No public toots found for this user.")
        return

    status = statuses[0]
    content = html_to_text_with_links(status.get("content", ""))
    url = status.get("url", "")
    boosts = status.get("reblogs_count", 0)
    replies = status.get("replies_count", 0)
    likes = status.get("favourites_count", 0)

    lines = [
        f"🐘 Latest toot from @{username}@{instance}:",
        f"{content}",
        f"{url}",
        f"🔁 {boosts}   💬 {replies}   ❤️ {likes}"
    ]
    bot.reply(msg, lines, ephemeral=False)

# ---------------- Urban Dictionary ----------------

UDICT_API_URL = "https://api.urbandictionary.com/v0/define?term={}"


@command("udict", role=Role.USER, aliases=["ud"])
async def udict_search(bot, sender_jid, nick, args, msg, is_room):
    """
    Search Urban Dictionary for a term.

    Usage:
        {prefix}udict <term>
        {prefix}ud <term>

    Example:
        {prefix}udict yeet
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Urban Dictionary lookup is disabled in this room.")
        return

    if not args:
        bot.reply(
            msg,
            f"🟡️ Usage: {config.get('prefix', ',')}udict <term>"
        )
        return

    term = " ".join(args).strip()
    url = UDICT_API_URL.format(term)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    log.warning("[UDICT] 🔴  Failed to fetch definition.")
                    bot.reply(msg, "🔴  Failed to fetch definition.")
                    return
                data = await resp.json()
    except Exception:
        log.exception("[UDICT] 🚨 Error fetching from Urban Dictionary.")
        bot.reply(msg, "🔴  Error fetching from Urban Dictionary.")
        return

    defs = data.get("list", [])
    if not defs:
        bot.reply(msg, f"ℹ️ No definitions found for '{term}'.")
        return

    entry = defs[0]
    definition = entry.get("definition", "").replace("\r", "").replace("\n", " ")
    example = entry.get("example", "").replace("\r", "").replace("\n", " ")
    thumbs_up = entry.get("thumbs_up", 0)
    thumbs_down = entry.get("thumbs_down", 0)
    permalink = entry.get("permalink", "")

    lines = [
        f"📚 Urban Dictionary: {term}",
        f"Definition: {definition}",
    ]
    if example:
        lines.append(f"Example: {example}")
    lines.append(f"👍 {thumbs_up}   👎 {thumbs_down}")
    if permalink:
        lines.append(permalink)

    bot.reply(msg, lines)

# ---------------- Wikipedia ----------------

WIKIPEDIA_API_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"


def fetch_wikipedia_summary(term):
    """
    Query the Wikipedia REST API and return extracted data, or None on error.
    """
    url = WIKIPEDIA_API_URL.format(requests.utils.quote(term))
    resp = requests.get(url, headers={"User-Agent": "envsbot/1.0"})
    if resp.status_code == 200:
        data = resp.json()
        title = data.get("title")
        summary = data.get("extract")
        url = data.get("content_urls", {}).get("desktop", {}).get("page")
        if title and summary and url:
            return title, summary, url
        # If it's a redirect/disambiguation, may contain other structure
        elif data.get("type") == "disambiguation" and "titles" in data:
            return data["titles"]["canonical"], "Disambiguation page", url
    return None

@command("wikipedia", role=Role.USER, aliases=["wiki"])
async def wikipedia_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Lookup a summary for a term using Wikipedia.

    Usage:
        {prefix}wikipedia <search term>
        {prefix}wiki <search term>
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Wikipedia lookup is disabled in this room.")
        return

    if not args:
        bot.reply(msg, "Usage: ,wikipedia <search term>")
        return

    term = " ".join(args)
    # Run blocking HTTP in executor
    result = await asyncio.get_event_loop().run_in_executor(
        None, fetch_wikipedia_summary, term
    )

    if result:
        title, summary, url = result
        lines = [
            f"📖 Wikipedia: {title}",
            summary,
            f"URL: {url}",
        ]
        bot.reply(msg, lines)
    else:
        bot.reply(msg, f"No Wikipedia summary found for '{term}'.")

# ---------------- Acronyms (Stands4) ----------------

STANDS4_API_BASE = "https://www.stands4.com/api.php"
STANDS4_API_KEY = config.get("stands4_api_key", "")  # Add to your config!
STANDS4_USER_ID = config.get("stands4_user_id", "")  # Add to your config!


@command("acronyms", role=Role.USER, aliases=["acro"])
async def acronyms_lookup(bot, sender_jid, nick, args, msg, is_room):
    """
    Look up the meaning of an acronym using the Stands4.com API.

    Usage:
        {prefix}acronyms NASA
        {prefix}acro NASA
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronym lookup is disabled in this room.")
        return

    if not args:
        bot.reply(msg, f"🟡️ Usage: {config.get('prefix', ',')}acronyms <ACRONYM>")
        return

    term = " ".join(args).strip()
    params = {
        "uid": STANDS4_USER_ID,
        "tokenid": STANDS4_API_KEY,
        "cmd": "acronym",
        "term": term,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(STANDS4_API_BASE, params=params, timeout=8) as resp:
                if resp.status != 200:
                    log.warning("[ACRONYM] 🔴  Failed to fetch acronym data.")
                    bot.reply(msg, "🔴  Failed to fetch acronym information.")
                    return
                xml_text = await resp.text()
                root = ET.fromstring(xml_text)

        defs = []
        for item in root.findall(".//result"):
            name = item.findtext("name", "")
            definition = item.findtext("definition", "")
            if name and definition:
                defs.append(f"• {name}: {definition}")
        if not defs:
            bot.reply(msg, f"ℹ️ No expansions found for '{term}'.")
            return

        lines = [f"🔤 Acronyms for '{term}':"] + defs[:3]
        if len(defs) > 3:
            lines.append("… (truncated)")
        bot.reply(msg, lines)
    except Exception:
        log.exception("[ACRONYM] 🚨 Error fetching from Stands4 API.")
        bot.reply(msg, "🔴  Error fetching acronym information.")


# ----------------- Acronyms (Stands4) ----------------

STANDS4_API_BASE = "https://www.stands4.com/services/v2/abbr.php"
STANDS4_API_KEY = config.get("stands4_api_key", "")  # Add to your config!
STANDS4_USER_ID = config.get("stands4_user_id", "")  # Add to your config!

@command("acronyms", role=Role.USER, aliases=["acro"])
async def acronyms_lookup(bot, sender_jid, nick, args, msg, is_room):
    """
    Look up the meaning of an acronym using the Stands4.com API. It's limited
    to 100 requests per day, so use wisely!

    Usage:
        {prefix}acronyms NASA
        {prefix}acro NASA
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronym lookup is disabled in this room.")
        return

    if not STANDS4_API_KEY or not STANDS4_USER_ID:
        log.warning("[ACRONYM] 🟡️ Stands4 API credentials not configured.")
        bot.reply(msg, "🔴 Acronym lookup is not configured.")
        return

    if not args:
        bot.reply(msg, f"🟡️ Usage: {config.get('prefix', ',')}acronyms <ACRONYM>")
        return

    term = " ".join(args).strip()
    params = {
        "uid": STANDS4_USER_ID,
        "tokenid": STANDS4_API_KEY,
        "format": "xml",
        "term": term,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(STANDS4_API_BASE, params=params, timeout=8) as resp:
                if resp.status != 200:
                    log.warning("[ACRONYM] 🔴 Failed to fetch acronym data.")
                    bot.reply(msg, "🔴 Failed to fetch acronym information.")
                    return
                xml_text = await resp.text()

        root = ET.fromstring(xml_text)
        defs = []
        for item in root.findall(".//result"):
            found_term = item.findtext("term", "")
            definition = item.findtext("definition", "")
            category = item.findtext("category", "")
            if found_term and definition:
                if category:
                    defs.append(f"• {found_term}: {definition} [{category}]")
                else:
                    defs.append(f"• {found_term}: {definition}")

        if not defs:
            bot.reply(msg, f"ℹ️ No expansions found for '{term}'.")
            return

        lines = [f"🔤 Acronyms for '{term}':"] + defs[:6]
        if len(defs) > 6:
            lines.append("… (truncated)")
        bot.reply(msg, lines)

    except Exception:
        log.exception("[ACRONYM] 🚨 Error fetching from Stands4 API.")
        bot.reply(msg, "🔴 Error fetching acronym information.")


# ----------------- Information Plugin Toggle -----------------

@command("information", role=Role.MODERATOR)
async def information_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Toggle information plugin features in the current room.

    Usage:
        {prefix}information on|off|status
    """
    if not args:
        bot.reply(msg, f"Usage: {config.get('prefix', ',')}information on|off|status")
        return

    if is_room or _is_muc_pm(msg):
        handled = await handle_room_toggle_command(
            bot,
            msg,
            is_room,
            args,
            store_getter=get_info_store,
            key=INFO_KEY,
            label="Get Urban Dictionary summaries",
            storage="dict",
            log_prefix="[INFORMATION]",
        )
        if handled:
            return

    bot.reply(msg, "Usage: {prefix}information on|off|status (in a room or PM)")


async def get_info_store(bot):
    return bot.db.users.plugin("information")
