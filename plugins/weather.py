"""
Info plugin: Show the current weather for a user's location
configured in their vCard. Only works in groupchats or MUC DMs
where the user has a vCard with a LOCATION field.

IMPORTANT: You may need to turn the plugins usage on with the following
command in each room you want to use it in:
    {prefix}weather on

Commands:
    {prefix}weather <on|off|status>
    {prefix}weather [nick]
"""

import aiohttp
import logging
import urllib
from plugins import _core
from plugins import vcard
from utils.command import command, Role
from utils.config import config     # !!! DO NOT REMOVE! NEEDED FOR TESTS !!!
from plugins.rooms import JOINED_ROOMS

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "weather",
    "version": "0.5.0",
    "description": ("Gives weather according to users location (supports MUCs"
                    "and MUC DMs)"),
    "category": "info",
    "requires": ["_core", "rooms", "vcard"],
}

WEATHER_KEY = "WEATHER"

log = logging.getLogger(__name__)


async def get_display_name(bot, jid):
    store = bot.db.users.plugin("users")
    try:
        roomnicks = await store.get(jid, "roomnicks")
        for room in roomnicks or []:
            if room:
                display_name = roomnicks[room][0]
                break
    except Exception as e:
        log.warning(
            "[PROFILE] 🔴  Failed to get roomnicks for %s: %s",
            jid, e
        )
        display_name = "unknown"
    log.info(
        "[PROFILE] 👤 Profile lookup for self: %s",
        display_name
    )
    return display_name


def get_pm_target(sender_jid, nick):
    if hasattr(sender_jid, "bare"):
        bare_jid = sender_jid.bare
    else:
        bare_jid = str(sender_jid).split('/')[0]
    return bare_jid, nick


async def get_weather_store(bot):
    return bot.db.users.plugin("weather")


@command("weather", role=Role.USER, aliases=["w"])
async def weather_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the current weather for a users location set in their vCard. If
    the <nick> is omitted, your own location according to your vCard is
    used. Only works in groupchats or MUC DMs where the user has a vCard
    with a LOCATION and/or COUNTRY (CTRY) field set (must be public).

    Usage:
        {prefix}weather
        {prefix}weather <on|off|status>
        {prefix}weather <nick>
    """
    handled = await _core.handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_weather_store,
        key=WEATHER_KEY,
        label="Get weather",
        storage="dict",
        log_prefix="[WEATHER]",
    )
    if handled:
        return

    enabled_rooms = await _core._get_enabled_rooms(bot, WEATHER_KEY, "weather")

    if _core._is_muc_pm(msg):
        await _handle_weather_muc_pm(bot, msg, args, enabled_rooms)
        return

    if is_room:
        await _handle_weather_room(bot, msg, args, enabled_rooms)
        return

    await _handle_weather_dm(bot, msg, args)


async def _handle_weather_room(bot, msg, args, enabled_rooms):
    log.info(
        f"[WEATHER] Command invoked in room {msg['from'].bare} by "
        f"{msg['from'].resource} with args: {args}"
    )

    muc_jid = msg["from"].bare
    if muc_jid not in enabled_rooms:
        return

    nicks = JOINED_ROOMS.get(muc_jid, {}).get("nicks", {})
    target_nick = _resolve_room_target_nick(bot, msg, args)
    if target_nick is None:
        return

    if target_nick not in nicks:
        log.info(
            f"[WEATHER] Lookup failed: Nick '{target_nick}'"
            f" not found in room {muc_jid}"
        )
        prefix = "Your nick" if not args else "Nick"
        bot.reply(msg, f"🔴  {prefix} '{target_nick}' not found in this room.")
        return

    jid = nicks[target_nick].get("jid", None)
    await _process_weather_for_jid(bot, msg, jid, target_nick,
                                   muc_jid, is_dm=False)


async def _handle_weather_muc_pm(bot, msg, args, enabled_rooms):
    log.info(
        f"[WEATHER] Command invoked in room {msg['from'].bare} by "
        f" {msg['from'].resource} with args: {args}"
    )

    muc_jid = msg["from"].bare
    if muc_jid not in enabled_rooms:
        return

    nicks = JOINED_ROOMS.get(muc_jid, {}).get("nicks", {})
    target_nick = _resolve_muc_pm_target_nick(bot, msg, args)
    if target_nick is None:
        return

    if target_nick not in nicks:
        log.info(
            f"[WEATHER] Lookup failed: Nick '{target_nick}'"
            f" not found in room {muc_jid}"
        )
        prefix = "Your nick" if not args else "Nick"
        bot.reply(msg, f"🔴  {prefix} '{target_nick}' not found in this room.")
        return

    jid = nicks[target_nick].get("jid", None)
    await _process_weather_for_jid(bot, msg, jid, target_nick,
                                   muc_jid, is_dm=False)


async def _handle_weather_dm(bot, msg, args):
    target_nick = msg["from"].bare
    display_name = target_nick

    if args:
        log.warning(
            f"[WEATHER] Command invoked by '{target_nick}'"
            f" in DM with args: {args}"
        )
        bot.reply(
            msg,
            "🔴  In a DM, you cannot specify a different nick."
            "Just use the command without arguments to get your weather.",
        )
        return

    try:
        vcard_data = await vcard.get_user_vcard(bot, msg, target_nick)
        locality, region, country = _extract_location_fields(vcard_data)
    except Exception as e:
        log.warning(f"[WEATHER] Failed to get vCard fields for"
                    f" {target_nick}: {e}")
        bot.reply(msg, "🔴  Failed to retrieve your vCard information.")
        return

    await _reply_with_weather(bot, msg, display_name,
                              locality, region, country)


def _resolve_room_target_nick(bot, msg, args):
    if args:
        return " ".join(args).strip()

    target_nick = msg.get("mucnick") or getattr(msg["from"], "resource", None)
    if not target_nick:
        bot.reply(msg, "🔴  Couldn't determine your nickname.")
        return None
    return target_nick


def _resolve_muc_pm_target_nick(bot, msg, args):
    if args:
        return " ".join(args).strip()

    target_nick = getattr(msg["from"], "resource", None)
    if not target_nick:
        bot.reply(msg, "🔴  Couldn't determine your nickname.")
        return None
    return target_nick


def _extract_location_fields(vcard_data):
    return (
        vcard_data.get("LOCALITY", None),
        vcard_data.get("REGION", None),
        vcard_data.get("CTRY", None),
    )


async def _process_weather_for_jid(bot, msg, jid, target_nick, muc_jid, is_dm):
    display_name = target_nick
    try:
        vcard_data = await vcard.get_user_vcard(bot, msg, jid)
        locality, region, country = _extract_location_fields(vcard_data)
    except Exception as e:
        log.warning(f"[WEATHER] Failed to get vCard fields for"
                    f" {target_nick}: {e}")
        bot.reply(
            msg,
            f"🔴  Failed to retrieve vCard information for '{target_nick}'.",
        )
        return

    log.info(f"[VCARD] vCard for '{target_nick}' ({muc_jid}) received.")
    await _reply_with_weather(bot, msg, display_name, locality,
                              region, country)


async def _reply_with_weather(bot, msg, display_name, locality,
                              region, country):
    location = _select_location(locality, region, country)

    log.info(f"[WEATHER] Location for {display_name}: {location}")

    if not location or location.strip() == "":
        bot.reply(msg, f"🟡️ No LOCATION in vCard for {display_name}.")
        return

    enc_location = urllib.parse.quote(location, safe="")
    url = f"https://wttr.in/{enc_location}?format=4&m"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    bot.reply(msg, f"🌦️ Failed to fetch weather for"
                                   f" {display_name}.")
                    log.warning(
                        f"[WEATHER] 🌦️ HTTP error {resp.status} for"
                        f" {display_name} at {location}"
                    )
                    return
                weather = await resp.text()
    except Exception:
        bot.reply(msg, f"🌦️ Failed to fetch weather for {display_name}.")
        log.warning(
            f"[WEATHER] 🌦️ Exception fetching weather for"
            f" {display_name} at {location}"
        )
        return

    weather_loc = weather.split(":")[0].strip()
    weather_desc = ":".join(weather.split(":")[1:]).strip()
    bot.reply(
        msg,
        f"🌤️ Weather for {display_name}: {weather_loc.title()}:"
        f" {weather_desc.strip()} ({location})",
        ephemeral=False,
    )


def _select_location(locality, region, country):
    location = None
    if country is not None:
        location = country
    if region is not None:
        location = region
    if locality is not None:
        location = locality
    if location is None:
        location = ""
    return location
