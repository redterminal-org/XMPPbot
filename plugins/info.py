"""
Info plugin.

This plugin provides various information commands:
- Wikipedia summary lookup
- Fetch latest toot from a Fediverse user
- Urban Dictionary term search
- Fetch an acronym's meaning, or add an acronym meaning not in the list

Commands:
    {prefix}wikipedia <search term> - lookup a summary for a term using
                                      Wikipedia
    {prefix}fediverse <@user@instance> - fetch latest public toot from a
                                         Fediverse user
    {prefix}udict <term> - search Urban Dictionary for a term
    {prefix}acronyms <ACRONYM> - look up a chat acronym (like 'lgtm')
    {prefix}acronym add <ACRONYM> <DESCRIPTION> - Will be reviewed before
                                                  addition
    {prefix}info on|off|status - to toggle in rooms
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
    "description": "Wikipedia, Fediverse, Urban Dictionary and acronym "
                   "lookup.",
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
                    log.warning(
                        "[FEDIVERSE] 🔴  Could not fetch user timeline."
                    )
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
    definition = entry.get("definition", "").replace("\r", "").replace(
        "\n", " ")
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
    resp = requests.get(url, headers={"User-Agent": "XMPPBot/1.0"})
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

# --- Configuration ---
SLANG_CSV = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "chat_slang.csv"
)
SLANG_ADDITIONS_CSV = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "slang_additions.csv"
)
SLANG_REMOVALS_CSV = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "slang_removals.csv"
)

log = logging.getLogger(__name__)


# --- CSV helpers ---

def load_main_definitions():
    """Load all acronyms and their descriptions from main CSV only."""
    defs = {}
    if os.path.exists(SLANG_CSV):
        with open(SLANG_CSV, encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    key = row[0].strip().lower()
                    desc = row[1].strip()
                    defs.setdefault(key, []).append(desc)
    return defs


def all_main_descriptions(acronym):
    results = []
    seen = set()
    for d in load_main_definitions().get(acronym.lower().strip(), []):
        norm = d.strip().lower()
        if norm not in seen:
            results.append(d.strip())
            seen.add(norm)
    return results


def addition_exists(acronym, description):
    acronym = acronym.lower().strip()
    description = description.lower().strip()
    if os.path.exists(SLANG_ADDITIONS_CSV):
        with open(SLANG_ADDITIONS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    abbr, desc = row[0].strip().lower(), row[1].strip().lower()
                    if abbr == acronym and desc == description:
                        return True
    return False


def removal_exists(acronym, description):
    acronym = acronym.lower().strip()
    description = description.lower().strip()
    if os.path.exists(SLANG_REMOVALS_CSV):
        with open(SLANG_REMOVALS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    abbr, desc = row[0].strip().lower(), row[1].strip().lower()
                    if abbr == acronym and desc == description:
                        return True
    return False


def description_exists_in_main(acronym, description):
    acronym = acronym.lower().strip()
    description = description.lower().strip()
    if os.path.exists(SLANG_CSV):
        with open(SLANG_CSV, encoding='utf-8') as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    abbr, desc = row[0].strip().lower(), row[1].strip().lower()
                    if abbr == acronym and desc == description:
                        return True
    return False


def delete_from_csv(filename, matchfunc):
    removed = 0
    kept = []
    if os.path.exists(filename):
        with open(filename, encoding="utf-8") as f:
            for row in csv.reader(f):
                if not matchfunc(row):
                    kept.append(row)
                else:
                    removed += 1
        with open(filename, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(kept)
    return removed


# --- Acronym Commands ---

@command("acronyms", aliases=["acro", "acronym"], role=Role.USER)
async def acronyms_cmd(bot, sender, nick, args, msg, is_room):
    """
    Look up all definitions of a chat acronym from the main list.

    Usage:
        {prefix}acronyms <acronym>
        {prefix}acro <acronym>
        {prefix}acronym <acronym>
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronyms are disabled in this room.")
        return

    if not args:
        return bot.reply(
            msg,
            f"Usage: {bot.prefix}acronyms <acronym>"
        )
    query = args[0].strip().lower()
    definitions = all_main_descriptions(query)
    if definitions:
        lines = [f"{query.upper()}: {d}" for d in definitions]
        log.info(
            f"[ACRONYMS] Returned {len(definitions)} definitions for "
            f"acronym '{query}' from main list."
        )
        return bot.reply(msg, lines)
    else:
        log.info(
            f"[ACRONYMS] User '{sender}' query '{query}' not found in main "
            f"database."
        )
        return bot.reply(
            msg,
            f"Sorry, '{query}' is not defined in my slang database."
        )


@command("acronyms add", aliases=["acro add", "acronym add"], role=Role.USER)
async def acronyms_add_cmd(bot, sender, nick, args, msg, is_room):
    """
    Suggest a new acronym/description. Entry will be reviewed by admins
    before becoming visible.

    Usage:
        {prefix}acronyms add <acronym> <description>
        {prefix}acro add <acronym> <description>
        {prefix}acronym add <acronym> <description>
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronyms are disabled in this room.")
        return

    if len(args) < 2:
        return bot.reply(
            msg,
            f"Usage: {bot.prefix}acronyms add <acronym> <description>"
        )
    abbreviation = args[0].strip()
    description = " ".join(args[1:]).strip()
    if description_exists_in_main(abbreviation, description):
        log.info(
            f"[ACRONYMS] {sender} tried to queue existing main def: "
            f"{abbreviation}:{description}"
        )
        return bot.reply(
            msg,
            f"The definition for '{abbreviation}' already exists in the "
            f"database."
        )
    if addition_exists(abbreviation, description):
        log.info(
            f"[ACRONYMS] {sender} tried to queue existing pending addition: "
            f"{abbreviation}:{description}"
        )
        return bot.reply(
            msg,
            "This suggestion is already awaiting admin review."
        )
    os.makedirs(os.path.dirname(SLANG_ADDITIONS_CSV), exist_ok=True)
    with open(SLANG_ADDITIONS_CSV, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([abbreviation, description, nick or sender])
    log.info(
        f"[ACRONYMS] Queued new addition by {sender}/{nick}: "
        f"{abbreviation}:{description}"
    )
    return bot.reply(
        msg,
        f"Suggestion for '{abbreviation}' was queued for admin review. "
        f"Thank you!"
    )


@command("acronyms remove", aliases=["acro remove", "acronym remove"],
         role=Role.USER)
async def acronyms_remove_cmd(bot, sender, nick, args, msg, is_room):
    """
    Suggest the removal of an existing acronym/description pair. Entry will
    be reviewed by admins.

    Usage:
        {prefix}acronyms remove <acronym> <description>
        {prefix}acro remove <acronym> <description>
        {prefix}acronym remove <acronym> <description>
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronyms are disabled in this room.")
        return

    if len(args) < 2:
        return bot.reply(
            msg,
            f"Usage: {bot.prefix}acronyms remove <acronym> <description>"
        )
    abbreviation = args[0].strip()
    description = " ".join(args[1:]).strip()
    if not description_exists_in_main(abbreviation, description):
        return bot.reply(
            msg,
            "That definition doesn't exist in the main list."
        )
    if removal_exists(abbreviation, description):
        log.info(
            f"[ACRONYMS] {sender} tried to queue existing pending removal: "
            f"{abbreviation}:{description}"
        )
        return bot.reply(
            msg,
            "This removal is already awaiting admin review."
        )
    os.makedirs(os.path.dirname(SLANG_REMOVALS_CSV), exist_ok=True)
    with open(SLANG_REMOVALS_CSV, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([abbreviation, description, nick or sender])
    log.info(
        f"[ACRONYMS] Queued new removal by {sender}/{nick}: "
        f"{abbreviation}:{description}"
    )
    return bot.reply(
        msg,
        f"Removal suggestion for '{abbreviation}' was queued for admin "
        f"review. Thank you!"
    )


@command("acronyms list", aliases=["acro list", "acronym list"],
         role=Role.ADMIN)
async def acronyms_list_cmd(bot, sender, nick, args, msg, is_room):
    """
    Display pending slang additions and removals with proposer nicknames for
    admin review.

    Usage:
        {prefix}acronyms list
        {prefix}acro list
        {prefix}acronym list
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronyms are disabled in this room.")
        return

    addition_lines = []
    removal_lines = []
    if os.path.exists(SLANG_ADDITIONS_CSV):
        with open(SLANG_ADDITIONS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 3:
                    addition_lines.append(
                        f"{row[0].upper()}: {row[1]} (by {row[2]})"
                    )
    if os.path.exists(SLANG_REMOVALS_CSV):
        with open(SLANG_REMOVALS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 3:
                    removal_lines.append(
                        f"{row[0].upper()}: {row[1]} (by {row[2]})"
                    )
    log.info(
        f"[ACRONYMS] Admin {sender} reviewed {len(addition_lines)} "
        f"additions and {len(removal_lines)} removals."
    )
    sections = []
    if addition_lines:
        sections.append("Pending Additions:\n" + "\n".join(addition_lines))
    else:
        sections.append("No pending additions.")
    if removal_lines:
        sections.append("Pending Removals:\n" + "\n".join(removal_lines))
    else:
        sections.append("No pending removals.")
    bot.reply(msg, "\n\n".join(sections))


@command("acronyms merge", aliases=["acro merge", "acronym merge"],
         role=Role.ADMIN)
async def acronyms_merge_cmd(bot, sender, nick, args, msg, is_room):
    """
    Admin command to apply pending additions and removals to the main slang
    database.

    Usage:
        {prefix}acronyms merge
        {prefix}acro merge
        {prefix}acronym merge
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronyms are disabled in this room.")
        return

    main_entries = []
    if os.path.exists(SLANG_CSV):
        with open(SLANG_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    acro = row[0].strip()
                    desc = row[1].strip()
                    main_entries.append([acro, desc])
    # Removals
    removals = set()
    if os.path.exists(SLANG_REMOVALS_CSV):
        with open(SLANG_REMOVALS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    acro, desc = row[0].strip(), row[1].strip()
                    removals.add((acro.lower(), desc.lower()))
    kept_entries = [
        row for row in main_entries
        if (row[0].lower(), row[1].lower()) not in removals
    ]
    removed_count = len(main_entries) - len(kept_entries)
    # Additions
    new_add_count = 0
    if os.path.exists(SLANG_ADDITIONS_CSV):
        with open(SLANG_ADDITIONS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    acro, desc = row[0].strip(), row[1].strip()
                    key = (acro.lower(), desc.lower())
                    if key not in {
                        (row[0].lower(), row[1].lower())
                        for row in kept_entries
                    }:
                        kept_entries.append([acro, desc])
                        new_add_count += 1
                        log.info(
                            f"[ACRONYMS] Added new slang: {acro}:{desc}"
                        )
    with open(SLANG_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(kept_entries)
    if os.path.exists(SLANG_ADDITIONS_CSV):
        os.remove(SLANG_ADDITIONS_CSV)
    if os.path.exists(SLANG_REMOVALS_CSV):
        os.remove(SLANG_REMOVALS_CSV)
    log.info(
        f"[ACRONYMS] Admin {sender} merged: +{new_add_count} additions, "
        f"-{removed_count} removals."
    )
    bot.reply(
        msg,
        f"Merged {new_add_count} additions and {removed_count} removals "
        f"into the slang database."
    )


@command("acronyms delete", aliases=["acro delete", "acronym delete"],
         role=Role.ADMIN)
async def acronyms_delete_cmd(bot, sender, nick, args, msg, is_room):
    """
    Admin command to delete from the suggestions/removals queue by
    (acronym, description) or by nick.

    Usage:
        {prefix}acronyms delete <acronym> <description>
        {prefix}acro delete <acronym> <description>
        {prefix}acronym delete <acronym> <description>
        {prefix}acronyms delete <nick>
        {prefix}acro delete <nick>
        {prefix}acronym delete <nick>
    """
    enabled_rooms = await _get_enabled_rooms(bot, INFO_KEY, "information")
    if msg["from"].bare not in enabled_rooms and (is_room or _is_muc_pm(msg)):
        bot.reply(msg, "ℹ️ Acronyms are disabled in this room.")
        return

    if not args:
        return bot.reply(
            msg,
            f"Usage: {bot.prefix}acronyms delete <acronym> <description> OR "
            f"{bot.prefix}acronyms delete <nick>"
        )
    total_removed = 0
    if len(args) == 1:
        # Delete all additions/removals made by that nick
        nick_arg = args[0].strip().lower()
        for fname in (SLANG_ADDITIONS_CSV, SLANG_REMOVALS_CSV):
            def matchfunc(row):
                return len(row) >= 3 and row[2].strip().lower() == nick_arg
            removed = delete_from_csv(fname, matchfunc)
            if removed:
                log.info(
                    f"[ACRONYMS] Admin {sender} deleted {removed} entries "
                    f"from {fname} for nick {nick_arg}"
                )
            total_removed += removed
        if total_removed:
            bot.reply(
                msg,
                f"Deleted {total_removed} entries for nick "
                f"'{args[0].strip()}' from pending additions/removals."
            )
        else:
            bot.reply(
                msg,
                f"No pending additions/removals found for nick "
                f"'{args[0].strip()}'."
            )
    else:
        abbreviation = args[0].strip().lower()
        description = " ".join(args[1:]).strip().lower()
        for fname in (SLANG_ADDITIONS_CSV, SLANG_REMOVALS_CSV):
            def matchfunc(row):
                return (
                    len(row) >= 2 and
                    row[0].strip().lower() == abbreviation and
                    row[1].strip().lower() == description
                )
            removed = delete_from_csv(fname, matchfunc)
            if removed:
                log.info(
                    f"[ACRONYMS] Admin {sender} deleted {removed} entries "
                    f"from {fname} for {abbreviation}:{description}"
                )
            total_removed += removed
        if total_removed:
            bot.reply(
                msg,
                f"Deleted {total_removed} entries for "
                f"'{abbreviation}: {description}' from pending "
                f"additions/removals."
            )
        else:
            bot.reply(
                msg,
                f"No pending addition/removal found for "
                f"'{abbreviation}: {description}'."
            )

# ----------------- Information Plugin Toggle -----------------


@command("info", role=Role.MODERATOR)
async def information_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Toggle info plugin features in the current room.

    Usage:
        {prefix}info on|off|status
    """
    if not args:
        bot.reply(
            msg,
            f"Usage: {config.get('prefix', ',')}info on|off|status"
        )
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

    bot.reply(
        msg,
        "Usage: {prefix}information on|off|status (in a room or PM)"
    )


async def get_info_store(bot):
    return bot.db.users.plugin("information")
