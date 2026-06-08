"""
vCard Lookup Plugin

This plugin allows users to request the fullname, nicknames, birthday,
notes, organisations and urls from their own or others vCard (if public).

The only exception is the "timezone", which has to be set explicitly with the
"{prefix}tz set <IANA timezone>".

You can get your own timezone from the list at:
https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
Use the "TZ identiier" from the list.

The weather plugin now uses the "LOCATION" and/or "CTRY" (country) fields from
your vCard to determine the location for weather reports, if set. If you have
more than one address the first one found will be used.

IMPORTANT: You may have to activate the vcard commands if not activated by
default with the command:
    {prefix}vcard on

"""

import logging
import textwrap
import pytz
import datetime
import urllib
from slixmpp.exceptions import IqError

from plugins import _core

from utils.command import command, Role
from utils.config import config
from plugins.rooms import JOINED_ROOMS

VCARD_KEY = "VCARD"

PLUGIN_META = {
    "name": "vcard",
    "version": "0.5.0",
    "description":
    "Lookup and display vCard of a MUC occupant by MUC JID only",
    "category": "info",
    "requires": ["rooms", "_core"],
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Expose get_user_vcard as get_profile for easier access across plugins
# Usage:
#   vcard = await core.get_profile(bot, msg, jid)
# Parameters:
#   - bot: The bot instance (for database access)
#   - msg: The message object (for context and replying)
#   - jid: The JID of the user whose profile to fetch
# Fields:
#   - vcard['FN'] - Full name
#   - vcard['NICKNAME'] - Nickname
#   - vcard['BDAY'] - Birthday
#   - vcard['URL'] - URLs
#   - vcard['ORG'] - Organization
#   - vcard['NOTE'] - Notes
#   - vcard['EMAIL'] - Emails
#   - vcard['LOCALITY'] - Locality
#   - vcard['REGION'] - Region
#   - vcard['COUNTRY'] - Country
#   - vcard['TZ'] - Timezone
# ----------------------------------------------------------------------
async def get_user_vcard(bot, msg, jid=None):
    """ Fetch and return the vCard information for a user.

    This function retrieves the vCard for the specified JID (or the sender
    if not provided), formats the vCard data, and adds the user's timezone
    from the database if available.

    Args:
        bot: The bot instance.
        msg: The message object (context for resolving JID if not provided).
        jid: (Optional) The JID of the user whose vCard to fetch.
             If None, resolves from msg.

    Returns:
        dict: A dictionary containing vCard fields (e.g., FN, NICKNAME,
        BDAY, URL, ORG, NOTE, EMAIL, LOCALITY, REGION, COUNTRY, TZ).
              The "TZ" field is populated from the database if available.
    """
    vcard_info = await get_vcard(bot, msg, jid)
    _, _vcard = _format_vcard_reply(vcard_info, None, None)

    # add Timezone from DB if available
    timezone = None
    jid, _, _ = await _core.get_real_jid(bot, msg)
    if jid is not None:
        timezone = await _core._get_user_timezone(bot, str(jid))
    else:
        jid, _, _ = await _core.get_real_jid(bot, msg)
        timezone = await _core._get_user_timezone(bot, str(jid))
    _vcard["TZ"] = timezone

    return _vcard


async def vcard_field(bot, msg, target_nick, field, is_room=False):
    """
    Helper to fetch a specific vCard field(s) for a given nick.
    Must be called from MUC PM or groupchat context with a valid
    target_nick present in the room.

    Supports fields: "FN", "NICKNAME", "BDAY", "TIMEZONE", "URL", "ORG",
    "NOTE", "EMAIL".
    Returns "None" if field is not present.
    """
    if field not in ["FN", "NICKNAME", "BDAY", "TIMEZONE", "URL", "NICKNAME",
                     "ORG", "NOTE", "EMAIL", "LOCALITY", "CTRY"]:
        log.warning("[VCARD] 🔴  Invalid vCard field requested: %s", field)
        return None
    if field == "TIMEZONE":
        if not is_room and not _core._is_muc_pm(msg):
            jid = msg["from"].bare
        else:
            jid = _core.get_real_jid_from_occupant(bot, msg, target_nick)
        if not jid:
            log.warning(f"[VCARD] 🔴  Nick '{target_nick}' not found in room"
                        f"'{msg['from'].bare}' for TIMEZONE lookup")
            return None
        value = await _core._get_user_timezone(bot, str(jid))
        if jid == msg["from"].bare:
            log.info(f"[VCARD] TIMEZONE lookup for sender's own JID '{
                     jid}': {value}")
        else:
            log.info(f"[VCARD] TIMEZONE lookup for nick '{target_nick}'"
                     f" with JID '{jid}' in room"
                     f"'{msg['from'].bare}': {value}")
        if not value:
            return None
        return value
    vcard_info = await get_vcard(bot, msg, jid=jid)
    _, vcard = _format_vcard_reply(vcard_info, None, None)
    return vcard[field]


@command("timezone set", role=Role.USER, aliases=["tz set"])
async def set_timezone(bot, sender_jid, nick, args, msg, is_room):
    """
    Set your TIMEZONE in Linux format eg. for '{prefix}time [nick]' command.

    Check your timezone at:
    https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
    Use the "TZ identiier" from the list.

    Usage:
        {prefix}timezone set <timezone>
        {prefix}tz set <timezone>

    Example:
        {prefix}timezone set Europe/Berlin
        {prefix}tz set Alaska/Anchorage
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    if not is_room and not _core._is_muc_pm(msg):
        jid = msg["from"].bare
    else:
        jid, _, _ = await _core.get_real_jid(bot, msg)
    log.info("[VCARD] ✅ set_timezone called by %s", jid)
    if not await _core._check_user_exists(bot, jid, msg):
        return
    if not args or len(args) != 1:
        log.warning("[VCARD] 🔴  TIMEZONE missing/invalid args for %s",
                    jid)
        bot.reply(
            msg,
            f"🟡️ Usage: {config.get('prefix', ',')}tz set <timezone>",
        )
        return
    timezone = args[0].strip()
    try:
        if timezone not in pytz.all_timezones:
            raise ValueError
    except Exception:
        log.warning("[VCARD] 🔴  Invalid timezone for %s: %s", jid,
                    timezone)
        bot.reply(
            msg,
            "🟡️ Invalid timezone. Use a valid IANA timezone, "
            "e.g. Europe/Berlin.",
        )
        return
    store = await get_vcard_store(bot)
    await store.set(str(jid), "TIMEZONE", timezone)
    log.info("[VCARD] ✅ TIMEZONE set for %s: %s", jid, timezone)
    bot.reply(msg, f"✅ TIMEZONE set to: {timezone}")


async def _format_vcard_field_for_nick(field, label, values,
                                       display_name, rooms=None):
    def indent_lines(lns, indent="    "):
        ln = [lns[0]] + [indent + li if li.strip() else li for li in lns[1:]]
        return ln

    if field == "URL":
        lines = []
        if rooms:
            lines.append(f"{label} - {display_name} in {', '.join(rooms)}:")
        else:
            lines.append(f"{label} - {display_name}:")
        if values and isinstance(values, list):
            for v in values:
                lines.append(f"    • {urllib.parse.unquote(v)}")
        else:
            lines.append("    • —")
        return lines
    elif field in ["EMAIL", "NICKNAME", "ORG", "NOTE"]:
        lines = []
        if rooms:
            lines.append(f"{label} - {display_name} in {', '.join(rooms)}:")
        else:
            lines.append(f"{label} - {display_name}:")
        if values and isinstance(values, list):
            for v in values:
                if field == "NOTE":
                    # Preserve newlines in notes, wrap and indent
                    # each paragraph after the bullet
                    note_paragraphs = v.splitlines() or [""]
                    for i, para in enumerate(note_paragraphs):
                        wrapped = textwrap.wrap(para, width=70)
                        if not wrapped:
                            wrapped = [""]
                        for j, line in enumerate(wrapped):
                            if i == 0 and j == 0:
                                lines.append(f"    • {line}")
                            else:
                                lines.append(f"      {line}")
                else:
                    lines.append(f"    • {v}")
        else:
            lines.append("    • —")
        return lines
    else:
        # For any other field, output the value(s) in a readable way
        lines = []
        if rooms:
            lines.append(f"{label} - {display_name} in {', '.join(rooms)}:")
        else:
            lines.append(f"{label} - {display_name}:")
        if values is None or values == "" or values == []:
            lines.append("    • —")
        elif isinstance(values, list):
            for v in values:
                lines.append(f"    • {v}")
        else:
            lines.append(f"    • {values}")
        return lines


async def _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           field, label):
    """
    Helper to fetch and display a profile field for a user nick.
    """
    # 1. Room context (groupchat) or MUC PM: lookup nick in room
    if (is_room or _core._is_muc_pm(msg)) and args:
        target_nick = " ".join(args).strip()
        room = msg["from"].bare
        joined = JOINED_ROOMS.get(room, {})
        nicks = joined.get("nicks", {})
        nick_info = nicks.get(target_nick)
        if not nick_info:
            log.warning("[VCARD] 🔴  Nick '%s' not found in room '%s'",
                        target_nick, room)
            bot.reply(msg, f"🔴  Nick '{target_nick}' not found in this room.")
            return
        if field == "TIMEZONE":
            jid = nick_info.get("jid")
            value = await _core._get_user_timezone(bot, str(jid))
            log.info(f"[VCARD] TIMEZONE lookup for nick '{target_nick}'"
                     f" with JID '{jid}' in room '{room}': {value}")
        else:
            jid = nick_info.get("jid")
            vcard = await get_user_vcard(bot, msg, jid)
            value = vcard[field]
        if value is None or value == "" or value == []:
            log.warning("[VCARD] 🔴  No vCard field '%s' for nick '%s'"
                        " in room '%s'",
                        label, target_nick, room)
            bot.reply(msg, f"🔴  No {label} found in vCard for nick '{
                      target_nick}'.")
            return
        display_name = target_nick
        log.info(f"[VCARD] {sender_jid} looking up {field} for "
                 f"'{target_nick}'")
        if value is None or value == "" or value == []:
            log.warning("[VCARD] 🔴  No %s for requested user '%s'",
                        field, target_nick)
            bot.reply(msg, f"ℹ️ No {label} set for nick '{target_nick}'.")
            return
        if field in ["FN", "NICKNAME", "BDAY", "TIMEZONE", "URL", "NICKNAME",
                     "ORG", "NOTE", "EMAIL"]:
            lines = await _format_vcard_field_for_nick(field, label,
                                                       value,
                                                       display_name,
                                                       [room])

            bot.reply(msg, lines)
        return
    # 2. Request own vCard information
    elif (is_room or _core._is_muc_pm(msg)) and not args:
        target_nick = msg["from"].resource
        room = msg["from"].bare
        joined = JOINED_ROOMS.get(room, {})
        nicks = joined.get("nicks", {})
        nick_info = nicks.get(target_nick)
        if not nick_info:
            log.warning("[VCARD] 🔴  Nick '%s' not found in room '%s'",
                        target_nick, room)
            bot.reply(msg,
                      f"🔴  Your Nick '{target_nick}' not found in this room.")
            return
        jid = nick_info.get("jid")
        if field == "TIMEZONE":
            value = await _core._get_user_timezone(bot, str(jid))
        else:
            vcard = await get_user_vcard(bot, msg, jid)
            if vcard[field] is None:
                log.warning("[VCARD] 🔴  No vCard field '%s' for nick '%s'"
                            "in room '%s'",
                            label, target_nick, room)
                bot.reply(msg, f"🔴  No {label} found in vCard for nick '{
                          target_nick}'.")
                return
            value = vcard[field]
        display_name = target_nick
        log.info(f"[VCARD] {sender_jid} looking up {field} for"
                 f"'{target_nick}'")
        if value is None or value == "" or value == []:
            log.warning("[VCARD] 🔴  No %s for requested user '%s'",
                        field, target_nick)
            bot.reply(msg, f"ℹ️ No {label} set for nick '{target_nick}'.")
            return
        if field in ["FN", "NICKNAME", "BDAY", "TIMEZONE", "URL", "NICKNAME",
                     "ORG", "NOTE", "EMAIL"]:
            lines = await _format_vcard_field_for_nick(field, label,
                                                       value,
                                                       display_name,
                                                       [room])
            bot.reply(msg, lines)

        else:
            bot.reply(msg, f"{label} for {display_name}: {value}")
        return

    # 2. Direct message to bot JID
    else:
        target_nick = msg["from"].bare
        room = "Direct Message"
        if args:
            log.info("[VCARD] Direct message with args from "
                     f"'{msg['from'].bare}'")
            bot.reply(msg, "🔴  In direct messages, you can only look up "
                           "your own vCard. Use the command without args.")
            return
        jid = msg["from"].bare
        if field == "TIMEZONE":
            jid = msg["from"].bare
            value = await _core._get_user_timezone(bot, str(jid))
        else:
            vcard = await get_user_vcard(bot, msg, msg["from"].bare)
            if vcard[field] is None:
                log.warning("[VCARD] 🔴  No vCard field '%s' for nick '%s'"
                            "in room '%s'",
                            label, target_nick, room)
                bot.reply(msg, f"🔴  No {label} found in vCard for nick '{
                          target_nick}'.")
                return
            value = vcard[field]
        display_name = target_nick
        log.info(f"[VCARD] {sender_jid} looking up {field} for"
                 f"'{target_nick}'")
        if value is None or value == "" or value == []:
            log.warning("[VCARD] 🔴  No %s for requested user '%s'",
                        field, target_nick)
            bot.reply(msg, f"ℹ️ No {label} set for nick '{target_nick}'.")
            return
        if field in ["FN", "NICKNAME", "BDAY", "TIMEZONE", "URL", "NICKNAME",
                     "ORG", "NOTE", "EMAIL"]:
            lines = await _format_vcard_field_for_nick(field, label,
                                                       value,
                                                       display_name,
                                                       [room])
            bot.reply(msg, lines)

        else:
            bot.reply(msg, f"{label} for {display_name}: {value}")
        return


async def get_vcard(bot, msg, jid=None):
    """
    Helper function to fetch vCard for a given JID using the xep_0054 plugin.
    """
    if jid is None:
        jid, _, _ = await _core.get_real_jid(bot, msg)
    try:
        vcard_plugin = bot.plugin.get("xep_0054", None)
        if not vcard_plugin:
            raise RuntimeError(
                "vCard support (xep_0054) is not enabled in this bot.")
        try:
            result = await vcard_plugin.get_vcard(jid=str(jid), cached=False,
                                                  timeout=10)
        except (IqError, Exception) as e:
            log.info(
                f"[VCARD] Exception while fetching vCard for '{jid}': {e}")
            result = None
        else:
            log.info(f"[VCARD] ✅ vCard fetch for '{jid}' completed")
        if not result:
            log.info(f"[VCARD] No vCard result for '{jid}'.")
            return None
        log.info(f"[VCARD] ✅ vCard for '{jid}' received.")
        return result["vcard_temp"]
    except Exception as e:
        log.error(f"[VCARD] Exception during vCard lookup for '{jid}': {e}")
        raise


async def get_info(bot, msg, jid=None):
    try:
        vcard = await get_user_vcard(bot, msg, jid)
        if not vcard:
            log.info(f"[VCARD] No vCard found for '{jid}'.")
            return None

    except Exception as e:
        log.error(f"[VCARD] Exception during vCard lookup for '{jid}': {e}")
        raise
    if not vcard:
        log.warning(
            f"[VCARD] Lookup failed: No vCard found for"
            f" sender's nick '{jid}'.")
        return None
    return vcard


def _get_all_field_values_by_tag(vcard, tag):
    """
    Extract all string values for the field 'tag' from vcard stanza children.
    """
    values = []
    for child in vcard.xml:
        # Check both namespace-tag form and plain tag
        if child.tag.endswith(tag) and child.text:
            values.append(child.text.strip())
    return values


def _get_nested_field_values_by_tag(vcard, parent_tag, child_tag):
    """Get all child_tag values under parent_tag elements in vcard XML."""
    values = []
    for field in vcard.xml:
        if field.tag.endswith(parent_tag):
            for child in field:
                if child.tag.endswith(child_tag) and child.text:
                    values.append(child.text.strip())
    return values


def _extract_email_addresses(vcard):
    """Extract USERID from all EMAIL fields in the vCard XML."""
    emails = []
    for child in vcard.xml:
        if child.tag.endswith("EMAIL"):
            # Find USERID child element within the EMAIL
            for email_child in child:
                if email_child.tag.endswith("USERID") and email_child.text:
                    # find USERID element and extract email address
                    for email_child in child:
                        if (email_child.tag.endswith("USERID")
                                and email_child.text):
                            emails.append(email_child.text.strip())
    return emails


def _format_vcard_reply(vcard, nick, muc_jid):
    # log vcard.xml to file
    # log.info("[VCARD] Raw vCard XML: %s",
    #          ET.tostring(vcard.xml, encoding="unicode"))
    c = {}
    lines = [f"📄 vCard for {nick} ({muc_jid}):"]

    fn = vcard.get("FN")
    c["FN"] = None
    if fn:
        lines.append(f"• Name: {fn}")
        c["FN"] = fn
    nicknames = _get_all_field_values_by_tag(vcard, "NICKNAME")
    c["NICKNAME"] = []
    if nicknames:
        lines.append(f"• Nicknames: {nicknames}")
        c["NICKNAME"] = nicknames
    c["BDAY"] = None
    bday = vcard["BDAY"]
    if bday:
        lines.append(f"• Birthday: {bday}")
        c["BDAY"] = bday

    # All URLs
    c["URL"] = []
    urls = _get_all_field_values_by_tag(vcard, "URL")
    if urls:
        lines.append("")
        c["URL"] = urls
    for url in urls:
        lines.append(f"• URL: {url}")

    c["ORG"] = []
    org_names = _get_nested_field_values_by_tag(vcard, "ORG", "ORGNAME")
    if org_names:
        lines.append("")
        for org in org_names:
            lines.append(f"• Organization: {org}")
            c["ORG"].append(org)

    # All Notes with wrapping
    c["NOTE"] = []
    notes = _get_all_field_values_by_tag(vcard, "NOTE")
    if notes:
        lines.append("")
        c["NOTE"] = notes
    for note in notes:
        note_paragraphs = note.splitlines() or [""]
        first_line = True
        for para in note_paragraphs:
            wrapped = textwrap.wrap(para, width=70)
            if not wrapped:
                wrapped = [""]
            for i, line in enumerate(wrapped):
                if first_line:
                    lines.append(f"• Note: {line}")
                    first_line = False
                else:
                    lines.append(f"        {line}")

    # Multiple emails support
    c["EMAIL"] = []
    emails = _extract_email_addresses(vcard)
    if emails:
        lines.append("")
        c["EMAIL"] = emails
        for email_addr in emails:
            lines.append(f"• Email: {email_addr}")

    adr = vcard.get("ADR")
    c["LOCALITY"] = None
    c["REGION"] = None
    c["CTRY"] = None
    if adr:
        lines.append("")  # Blank line before address
        locality = adr.get("LOCALITY")
        if locality:
            c["LOCALITY"] = locality
        region = adr.get("REGION")
        if region:
            c["REGION"] = region
        ctry = adr.get("CTRY")
        if ctry:
            c["CTRY"] = ctry
        vals = [val for val in (locality, region, ctry) if val]
        if vals:
            lines.append(f"• Address: {' '.join(vals)}")

    if len(lines) == 1:
        lines.append("  (no public vCard fields found)")
    return lines, c


async def get_vcard_store(bot):
    return bot.db.users.plugin("vcard")


@command("vcard", role=Role.USER, aliases=["v"])
async def vcard_command(bot, sender_jid, sender_nick, args, msg, is_room):
    """
    Look up the vCard of a user by MUC nick (MUC JID only), never real JID!

    Usage: {prefix}vcard [<nick>|on|off|status]

    IMPORTANT: You may have to activate the vcard commands if not activated
    by default with the command:
        {prefix}vcard on

    Usage:
        {prefix}vcard on|off|status
            - Enable, disable or check status of vCard commands in this room.
        {prefix}vcard [nick]
            - Look up the vCard of a user by their MUC nickname in this room.
              or omit the nick for your own vCard

    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    jid = None
    if is_room or _core._is_muc_pm(msg):
        handled = await _core.handle_room_toggle_command(
            bot,
            msg,
            is_room,
            args,
            store_getter=get_vcard_store,
            key=VCARD_KEY,
            label="Get vCard data",
            storage="dict",
            log_prefix="[VCARD]",
        )
        if handled:
            return

    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")

    if (is_room or _core._is_muc_pm(msg)) and args:
        target_nick = " ".join(args).strip()
        muc_jid = f"{msg['from'].bare}"
        if muc_jid not in enabled_rooms:
            return
        # Resolve JID for the target nick
        joined = JOINED_ROOMS.get(muc_jid, {})
        nicks = joined.get("nicks", {})
        nick_info = nicks.get(target_nick)
        if not nick_info:
            bot.reply(msg, f"🔴  Nick '{target_nick}' not found in this room.")
            return
        jid = nick_info.get("jid")
        if not jid:
            bot.reply(msg, f"🔴  Could not resolve JID for nick '{
                      target_nick}'.")
            return
    elif (is_room or _core._is_muc_pm(msg)) and not args:
        target_nick = msg["from"].resource
        muc_jid = f"{msg['from'].bare}"
        if muc_jid not in enabled_rooms:
            return
        # Resolve JID for the sender's own nick
        joined = JOINED_ROOMS.get(muc_jid, {})
        nicks = joined.get("nicks", {})
        nick_info = nicks.get(target_nick)
        if not nick_info:
            bot.reply(msg, f"🔴  Your Nick '{
                      target_nick}' not found in this room.")
            return
        jid = nick_info.get("jid")
        if not jid:
            bot.reply(msg, f"🔴  Could not resolve your JID for nick '{
                      target_nick}'.")
            return
    else:
        # DM context: lookup sender's own vCard by JID
        if args:
            log.info(f"[VCARD] Direct message with args from '{
                     msg['from'].bare}'")
            bot.reply(
                msg,
                "🔴  In direct messages, you can only look up your own vCard."
                " Use the command without args.")
            return
        jid = msg["from"].bare
        target_nick = jid
        muc_jid = "Direct Message"

    try:
        vcard_info = await get_vcard(bot, msg, jid=jid)

        if not vcard_info:
            bot.reply(msg, f"ℹ️ No vCard found for {target_nick} ({muc_jid}).")
            log.info(f"[VCARD] No vCard found for '{target_nick}' ({muc_jid})")
            return

        lines, vcard = _format_vcard_reply(vcard_info, target_nick, muc_jid)

        # add Timezone from DB if available
        timezone = None
        if is_room or _core._is_muc_pm(msg):
            if args:
                if jid:
                    timezone = await _core._get_user_timezone(bot, str(jid))
            else:
                jid, _, _ = await _core.get_real_jid(bot, msg)
                timezone = await _core._get_user_timezone(bot, str(jid))
        else:
            timezone = await _core._get_user_timezone(bot, str(jid))
        if timezone:
            if lines[-1] != "":
                lines.append("")  # Blank line before timezone
            lines.append(f"• Timezone: {timezone}")

        bot.reply(msg, lines)
    except Exception as e:
        bot.reply(msg, f"🔴 Failed to fetch vCard for {target_nick}: {e}")
        log.error(f"[VCARD] Exception during vCard lookup for '{
                  target_nick}' ({muc_jid}): {e}")


@command("fullname", role=Role.USER, aliases=["f"])
async def get_fullname(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the FULLNAME of a user from their vCard.

    Usage:
        {prefix}fullname [nick]
        {prefix}f [nick]

    Example:
        {prefix}fullname Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "FN", "Full Name")


@command("nicknames", role=Role.USER, aliases=["nicks"])
async def get_nicknames(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the nicknames from a user's vCard.

    Usage:
        {prefix}nicknames [nick]
        {prefix}nicks [nick]

    Example:
        {prefix}nicknames Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "NICKNAME", "Nicknames")


@command("timezone", role=Role.USER, aliases=["tz"])
async def get_timezone(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the TIMEZONE of a user from their DB entry (TZ not available
    on vCard).

    Usage:
        {prefix}timezone [nick]
        {prefix}tz [nick]

    Example:
        {prefix}timezone Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "TIMEZONE", "Timezone")


@command("organisations", role=Role.USER, aliases=["orgs"])
async def get_organisations(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the organisations from a user's vCard.

    Usage:
        {prefix}organisations [nick]
        {prefix}orgs [nick]

    Example:
        {prefix}orgs Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "ORG", "Organisations")


@command("notes", role=Role.USER)
async def get_notes(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the notes from a user's vCard.

    Usage:
        {prefix}notes [nick]

    Example:
        {prefix}notes Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "NOTE", "Notes")


@command("emails", role=Role.USER, aliases=["e"])
async def get_email(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the EMAILs of a user.

    Usage:
        {prefix}emails [nick]
        {prefix}e [nick]

    Example:
        {prefix}email Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "EMAIL", "Emails")


@command("urls", role=Role.USER, aliases=["u"])
async def get_urls(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the URLS of a user.

    Usage:
        {prefix}urls [nick]
        {prefix}u [nick]

    Example:
        {prefix}urls Envsi
    """
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    await _get_vcard_field(bot, sender_jid, nick, args, msg, is_room,
                           "URL", "URLs")


@command("birthday", role=Role.USER, aliases=["b"])
async def get_birthday(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the BIRTHDAY of a user and days until next birthday from their vCard.

    Usage:
        {prefix}birthday [nick]
        {prefix}b [nick]
    Example:
        {prefix}birthday Envsi
    """
    jid = None
    # Check, if command is allowed in this context (room or MUC PM)
    enabled_rooms = await _core._get_enabled_rooms(bot, VCARD_KEY, "vcard")
    if msg["from"].bare not in enabled_rooms and (is_room or
                                                  _core._is_muc_pm(msg)):
        return

    # 1. Room context (groupchat) or MUC PM: lookup nick in room
    if (is_room or _core._is_muc_pm(msg)) and args:
        target_nick = " ".join(args).strip()
        room = msg["from"].bare
        joined = JOINED_ROOMS.get(room, {})
        nicks = joined.get("nicks", {})
        nick_info = nicks.get(target_nick)
        if not nick_info:
            bot.reply(msg, f"🔴  Nick '{target_nick}' not found in this room.")
            return
        display_name = target_nick
        jid = nick_info.get("jid")
    elif (is_room or _core._is_muc_pm(msg)) and not args:
        target_nick = msg["from"].resource
        room = msg["from"].bare
        joined = JOINED_ROOMS.get(room, {})
        nicks = joined.get("nicks", {})
        nick_info = nicks.get(target_nick)
        if not nick_info:
            bot.reply(msg,
                      f"🔴  Your Nick '{target_nick}' not found in this room.")
            return
        jid = nick_info.get("jid")
        display_name = target_nick
    else:
        if args:
            log.info(f"[VCARD] Direct message with args from '{
                     msg['from'].bare}'")
            bot.reply(
                msg,
                "🔴  In direct messages, you can only look up your own"
                " birthday. Use the command without args.")
            return
        room = "Direct Message"
        jid = str(msg["from"].bare)
        display_name = jid

    vcard = await get_info(bot, msg, jid)
    value = None
    if vcard and vcard["BDAY"] is not None:
        value = vcard["BDAY"]
    if value is None or value == "" or value == []:
        bot.reply(msg, f"ℹ️ No Birthday set for {display_name}.")
        return

    # Calculate days until next birthday
    today = datetime.date.today()
    try:
        if len(value) == 10:  # YYYY-MM-DD
            month = int(value[5:7])
            day = int(value[8:10])
        elif len(value) == 5:  # MM-DD
            month = int(value[0:2])
            day = int(value[3:5])
        else:
            raise ValueError
        this_year = today.year
        next_birthday = datetime.date(this_year, month, day)
        if next_birthday < today:
            next_birthday = datetime.date(this_year + 1, month, day)
        days_left = (next_birthday - today).days
        days_str = f"{days_left} day{'s' if days_left != 1 else ''}"
        bot.reply(msg, f"🎂 Birthday for {display_name}: {value}"
                  + f" ({days_str} until next birthday)")
    except Exception:
        bot.reply(msg, f"🎂 Birthday for {display_name}: {value}")
