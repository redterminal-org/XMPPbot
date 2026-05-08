"""
Info plugin.

This plugin provides various information commands:
- Wikipedia summary lookup
- Fetch latest toot from a Fediverse user
- Urban Dictionary term search
- Fetch an acronym's meaning, or add an acronym meaning not in the list

Commands:
    {prefix}wikipedia <search term> - lookup a summary for a term using Wikipedia
    {prefix}fediverse <@user@instance> - fetch latest public toot from a
                                         Fediverse user
    {prefix}udict <term> - search Urban Dictionary for a term
    {prefix}acronyms <ACRONYM> - look up a chat acronym (like 'lgtm')
    {prefix}acronym add <ACRONYM> <DESCRIPTION> - Will be reviewed before addition
    {prefix}information on|off|status (to toggle in rooms)
"""

import aiohttp
import asyncio
import requests
import html
import logging
import re
import csv
import os

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
    "name": "info",
    "version": "0.5.0",
    "description": "Wikipedia, Fediverse, Urban Dictionary and acronym lookup.",
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


# ----------------- Chat Slang Lookup -----------------

SLANG_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "chat_slang.csv")
SLANG_ADDITIONS_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "slang_additions.csv")


def load_main_definitions():
    """Load all acronyms and all descriptions for each, from main CSV only."""
    defs = {}
    if os.path.exists(SLANG_CSV):
        with open(SLANG_CSV, encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    key = row[0].strip().lower()
                    desc = row[1].strip()
                    defs.setdefault(key, []).append(desc)
    else:
        log.warning(f"[ACRONYMS] Main slang CSV '{SLANG_CSV}' not found."
                    " Acronym lookup will be empty.")
    return defs


def all_main_descriptions(acronym):
    """Return all unique descriptions for acronym in main list."""
    results = []
    seen = set()
    for d in load_main_definitions().get(acronym.lower().strip(), []):
        norm = d.strip().lower()
        if norm not in seen:
            results.append(d.strip())
            seen.add(norm)
    return results


def description_exists_anywhere(acronym, description):
    """Check for (acronym, description-lower) in both files."""
    acronym = acronym.lower().strip()
    description = description.lower().strip()
    for fname in (SLANG_CSV, SLANG_ADDITIONS_CSV):
        if os.path.exists(fname):
            with open(fname, encoding="utf-8") as f:
                for row in csv.reader(f):
                    if len(row) >= 2:
                        abbr, desc = row[0].strip().lower(), row[1].strip().lower()
                        if abbr == acronym and desc == description:
                            return True
    return False


@command("acronyms", aliases=["acro"], role=Role.USER)
async def acronyms_cmd(bot, sender, nick, args, msg, is_room):
    """
    Look up all definitions of a chat acronym. If a definition doesn't exist,
    you can add it with '{prefix}acronym add <acronym> <description>'
    (will be reviewed before added to main list).

    Usage:
        {prefix}acronyms <abbr>
        {prefix}acro <abbr>
    """
    if not args:
        return bot.reply(msg, f"Usage: {config.get('prefix', ',')}acronyms <abbreviation or acronym>")
    query = args[0].strip().lower()
    definitions = all_main_descriptions(query)
    if definitions:
        lines = [f"{query.upper()}: {d}" for d in definitions]
        log.info(f"[ACRONYMS] Returned {len(definitions)} definitions"
                 f" for acronym '{query}' from main list.")
        return bot.reply(msg, lines)
    else:
        log.info(f"[ACRONYMS] User '{sender}' query '{query}' not found in main database.")
        return bot.reply(msg, f"Sorry, '{query}' is not defined in my slang database."
                              " Maybe you can add it with"
                              f"'{config.get('prefix', ',')}acronym add <abbreviation> <description>'")


@command("acronym add", role=Role.USER, aliases=["acro add"])
async def abbreviation_add_cmd(bot, sender, nick, args, msg, is_room):
    """
    Queue a new acronym/description (will be reviewed before added to the
    main list).

    Usage:
        {prefix}acronym add <abbreviation> <description>
        {prefix}acro add <abbreviation> <description>
    """
    if len(args) < 2:
        return bot.reply(msg, f"Usage: {config.get('prefix', ',')}acronym add"
                         "<abbreviation> <description>")
    abbreviation = args[0].strip()
    abbreviation = abbreviation.replace(",", " ")
    description = " ".join(args[1:]).strip()
    description = description.replace(",", " ")
    if description_exists_anywhere(abbreviation, description):
        log.info(f"[ACRONYMS] {sender} tried to queue existing def: {abbreviation}:{description}")
        return bot.reply(msg, f"'{abbreviation}' with that description is already in the database.")
    # Append to additions
    os.makedirs(os.path.dirname(SLANG_ADDITIONS_CSV), exist_ok=True)
    with open(SLANG_ADDITIONS_CSV, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([abbreviation, description])
    log.info(f"[ACRONYMS] Queued new abbreviation by {sender}: {abbreviation}:{description}")
    # Only confirm queuing to user, never output description details!
    return bot.reply(msg, f"Abbreviation '{abbreviation}' was queued for review. It will only appear after admin approval.")


@command("acronyms additions", role=Role.ADMIN, aliases=["acro additions"])
async def abbreviation_additions_cmd(bot, sender, nick, args, msg, is_room):
    """
    Show queued acronym additions (ADMIN only).

    Usage:
        {prefix}acronym additions
        {prefix}acro additions
    """
    lines = []
    if os.path.exists(SLANG_ADDITIONS_CSV):
        with open(SLANG_ADDITIONS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    lines.append(f"{row[0]}: {row[1]}")
    log.info(f"[ACRONYMS] Admin {sender} viewed the pending additions ({len(lines)} entries)")
    if lines:
        bot.reply(msg, "\n".join(lines))
    else:
        bot.reply(msg, "No pending abbreviation additions.")


@command("acronyms delete", role=Role.ADMIN, aliases=["acro delete"])
async def abbreviation_deladdition_cmd(bot, sender, nick, args, msg, is_room):
    """
    Delete a specific (acronym, description) from additions (ADMIN only).

    Usage:
        {prefix}acronym delete <abbreviation> <description>
        {prefix}acro delete <abbreviation> <description>
    """
    if len(args) < 2:
        return bot.reply(msg, (f"Usage: {config.get('prefix', ',')}acronym delete"
                               "<abbreviation> <description>"))
    abbreviation = args[0].strip().lower()
    description = " ".join(args[1:]).strip().lower()
    # Read all, re-write without the target pair
    entries = []
    found = False
    if os.path.exists(SLANG_ADDITIONS_CSV):
        with open(SLANG_ADDITIONS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    abbr, desc = row[0].strip().lower(), row[1].strip().lower()
                    if abbr == abbreviation and desc == description:
                        found = True
                        log.info(f"Admin {sender} deleted addition '{row[0]}: {row[1]}'")
                        continue
                    entries.append([row[0], row[1]])
        # Write back without target
        with open(SLANG_ADDITIONS_CSV, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(entries)
    if found:
        bot.reply(msg, f"Entry '{abbreviation}: {description}' removed from additions.")
        log.info(f"[ACRONYMS] Admin {sender} deleted an addition: {abbreviation}: {description}")
    else:
        bot.reply(msg, "No such entry in additions.")


@command("acronyms merge", role=Role.ADMIN, aliases=["acro merge"])
async def abbreviation_merge_cmd(bot, sender, nick, args, msg, is_room):
    """
    Merge queued abbreviation additions into main slang csv (ADMIN only).
    Please do so only after review.

    Usage:
        {prefix}acronym merge
        {prefix}acro merge
    """
    if not os.path.exists(SLANG_ADDITIONS_CSV):
        return bot.reply(msg, "No pending additions to merge.")
    main = {}
    if os.path.exists(SLANG_CSV):
        with open(SLANG_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    abbr = row[0].strip().lower()
                    desc = row[1].strip().lower()
                    main.setdefault(abbr, set()).add(desc)
    additions = []
    with open(SLANG_ADDITIONS_CSV, encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                abbr = row[0].strip()
                desc = row[1].strip()
                abbr_l = abbr.lower()
                desc_l = desc.lower()
                if abbr_l not in main or desc_l not in main[abbr_l]:
                    additions.append((abbr, desc))
                    main.setdefault(abbr_l, set()).add(desc_l)
    if not additions:
        os.remove(SLANG_ADDITIONS_CSV)
        log.info(f"[ACRONYMS] No new unique additions to merge; cleared additions on {sender}'s request.")
        return bot.reply(msg, "Nothing new to merge – all additions already present.")
    with open(SLANG_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(additions)
    os.remove(SLANG_ADDITIONS_CSV)
    log.info(f"[ACRONYMS] Admin {sender} merged {len(additions)} new abbreviations into chat_slang.csv")
    bot.reply(msg, f"Merged {len(additions)} additions into the main slang database.")


# ----------------- Information Plugin Toggle -----------------

@command("info", role=Role.MODERATOR)
async def information_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Toggle information plugin features in the current room.

    Usage:
        {prefix}info on|off|status
    """
    if not args:
        bot.reply(msg, f"Usage: {config.get('prefix', ',')}info on|off|status")
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
