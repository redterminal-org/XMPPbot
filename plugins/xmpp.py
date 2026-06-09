"""
XMPP utility commands plugin.

This plugin provides various commands for interacting with XMPP
servers and users, such as pinging a JID, querying service discovery info,
checking compliance scores, and performing DNS SRV lookups.

Commands:
    {prefix}x <on|off|status>       - Toggle usage of XMPP commands in a room
                                      or show status.
    {prefix}x help                  - Displays all available commands.
    {prefix}x version <domain>      - Shows the software version of an
                                      XMPP server (XEP-0092).
    {prefix}x items <domain|jid>    - Lists service items of an
                                      XMPP server (XEP-0030).
    {prefix}x contact <domain>      - Displays admin/contact information for a
                                      server (XEP-0030).
    {prefix}x info <domain|jid>     - Shows identities & features (XEP-0030).
    {prefix}x ping <domain|jid>     - Pings an XMPP entity (XEP-0199).
    {prefix}x uptime <domain>       - Shows the uptime of an XMPP server
                                      (XEP-0012).
    {prefix}x srv <domain>          - DNS SRV lookup.
    {prefix}x compliance <domain>   - Compliance score from
                                      compliance.conversations.im.
"""
import time
import slixmpp
import aiohttp
import asyncio
from utils.command import command, Role
from utils.config import config
from plugins._core import (
        handle_room_toggle_command,
        _is_muc_pm,
        JOINED_ROOMS,
)

XMPP_KEY = "XMPP"

PLUGIN_META = {
    "name": "xmpp",
    "version": "0.3.2",
    "description":
    "XMPP utility tools (ping, diagnostics, service discovery, DNS SRV, etc.)",
    "category": "tools",
    "requires": ["rooms", "_core"],
}

HELP_TEXT = """
XMPP Utility Commands:
  {prefix}x help                  - Show this help message
  {prefix}x <on|off|status>       - Toggle usage or show status
  {prefix}x version <domain>      - Show server software version (XEP-0092)
  {prefix}x items <domain|jid>    - List service items (XEP-0030)
  {prefix}x contact <domain>      - Show server contact information (XEP-0030)
  {prefix}x info <domain|jid>     - Show identities & features (XEP-0030)
  {prefix}x ping <domain|jid>     - Ping entity (XEP-0199)
  {prefix}x uptime <domain>       - Show server uptime (XEP-0012)
  {prefix}x srv <domain>          - DNS SRV lookup
  {prefix}x compliance <domain>   - Compliance score
""".format(prefix=config.get("prefix", ""))


async def get_xmpp_store(bot):
    return bot.db.users.plugin("xmpp")


def _resolve_target(bot, args, msg, is_room, nick):
    """
    Resolves the command argument to a valid XMPP JID target or room-nick,
    depending on current context (rooms, PM, etc).
    Returns (target, error_message) tuple.
    """
    if not args or len(args) < 1:
        return None, "Missing target JID or nick"
    target = args[0]
    if (is_room or (
        msg.get("type") in ("chat", "normal")
        and hasattr(msg["from"], "bare")
        and str(msg["from"].bare) in JOINED_ROOMS
    )):
        room = msg["from"].bare
        nicks = JOINED_ROOMS.get(room, {}).get("nicks", {})
        if target in nicks:
            return f"{room}/{target}", None
    return target, None


def get_domain_from_jid(arg):
    """
    Returns the domain part if an argument is a JID, otherwise returns the
    argument unchanged.
    """
    if "@" in arg:
        return arg.split("@", 1)[1]
    return arg


def inform_if_jid(msg, target, bot, command_name, domain_only=False):
    """
    If user gave a JID when a domain is required, inform the user.
    """
    if "@" in target:
        domain = get_domain_from_jid(target)
        if domain_only:
            bot.reply(msg, f"Note: '{command_name}' only works with domains."
                           f" Using '{domain}' from '{target}'.")
        return domain
    return target


def _validate_domain(domain: str) -> tuple[bool, str]:
    """
    Validate that a string is a valid domain name.

    Returns:
        (bool, str): (is_valid, error_message)
    """
    if not domain or not domain.strip():
        return False, "Domain cannot be empty"

    domain = domain.strip().lower()

    # Must contain at least one dot (example.com, not just "localhost")
    if '.' not in domain:
        return False, (f"'{domain}' is not a valid domain (must have at"
                       f" least one dot, e.g., example.com)")

    # Check each label
    labels = domain.split('.')
    for label in labels:
        if not label:
            return False, f"'{domain}' has empty labels (e.g., 'example..com')"
        if len(label) > 63:
            return False, (f"Label '{label}' in '{domain}' is too long"
                           f"(max 63 characters)")
        # Valid characters: a-z, 0-9, hyphen (not at start/end)
        if not all(c.isalnum() or c == '-' for c in label):
            return False, f"Label '{label}' contains invalid characters"
        if label.startswith('-') or label.endswith('-'):
            return False, f"Label '{label}' cannot start or end with hyphen"

    # TLD must be at least 2 characters
    if len(labels[-1]) < 2:
        return False, (f"'{domain}' has invalid TLD"
                       f" (must be at least 2 characters)")

    return True, ""


@command("xmpp", role=Role.USER, aliases=["x"])
async def cmd_xmpp(bot, sender_jid, nick, args, msg, is_room):
    """
    Toggle xmpp commands on or off or show status.

    Usage:
        {prefix}xmpp on|off|status - Toggle usage or show status
    """

    handled = await handle_room_toggle_command(
        bot,
        msg,
        is_room,
        args,
        store_getter=get_xmpp_store,
        key=XMPP_KEY,
        label="Use XMPP commands",
        storage="dict",
        log_prefix="[XMPP]",
    )
    if handled:
        return

    bot.reply(msg, "Usage: {prefix}xmpp <on|off|"
                   "status>".format(prefix=config.get("prefix", "")))
    return


@command("xmpp help", role=Role.USER, aliases=["x help"])
async def cmd_xmpp_help(bot, sender_jid, nick, args, msg, is_room):
    """
    Display help message with all available XMPP commands.

    Usage:
        {prefix}xmpp help
        {prefix}x help
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    bot.reply(msg, HELP_TEXT)


@command("xmpp version", role=Role.USER, aliases=["x version"])
async def cmd_xmpp_version(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the software version of an XMPP server (XEP-0092).
    Usage:
        {prefix}xmpp version <domain>
        {prefix}x version <domain>
    """
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if _should_block_xmpp_version(is_room, msg, enabled_rooms):
        return

    if not args or len(args) < 1:
        bot.reply(msg, "❌ Missing domain")
        return

    target = get_domain_from_jid(args[0])

    is_valid, error_msg = _validate_domain(target)
    if not is_valid:
        bot.reply(msg, f"❌ Invalid domain: {error_msg}")
        return

    if "@" in args[0]:
        bot.reply(
            msg,
            f"Note: 'version' only works with domains."
            f" Using '{target}' from '{args[0]}'."
        )

    try:
        result = await bot.plugin["xep_0092"].get_version(jid=target,
                                                          timeout=8)
        name, version, os_info = _extract_xmpp_version_info(result)

        if name and version:
            version_info = _format_xmpp_version_info(name, version, os_info)
            bot.reply(msg, f"ℹ️ Version for {target}: {version_info}")
        else:
            bot.reply(
                msg,
                f"ℹ️ {target} does not provide version"
                f" information via XEP-0092"
            )
    except slixmpp.exceptions.IqTimeout:
        bot.reply(msg, f"🔴 Version request to {target} timed out.")
    except slixmpp.exceptions.IqError as e:
        err_condition = _get_iq_error_condition(e)
        if err_condition == "service-unavailable":
            bot.reply(
                msg,
                f"🔴 {target} does not support version"
                f" requests (XEP-0092)."
            )
        else:
            bot.reply(msg, f"🔴 Version request failed: {err_condition}")
    except Exception as e:
        bot.reply(msg, f"🔴 Error: {e}")


def _should_block_xmpp_version(is_room, msg, enabled_rooms):
    return ((is_room or _is_muc_pm(msg))
            and msg["from"].bare not in enabled_rooms)


def _extract_xmpp_version_info(result):
    name, version, os_info = None, None, None

    if hasattr(result, "xml"):
        for child in result.xml:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag != "query":
                continue

            for elem in child:
                if "}" in elem.tag:
                    elem_tag = elem.tag.split("}")[-1]
                else:
                    elem_tag = elem.tag
                if elem_tag == "name":
                    name = elem.text
                elif elem_tag == "version":
                    version = elem.text
                elif elem_tag == "os":
                    os_info = elem.text

    return name, version, os_info


def _format_xmpp_version_info(name, version, os_info):
    version_info = f"**{name}** v{version}"
    if os_info:
        version_info += f" on {os_info}"
    return version_info


def _get_iq_error_condition(exc):
    err = exc.iq["error"]
    return err.get("condition", "unknown")


@command("xmpp uptime", role=Role.USER, aliases=["x uptime"])
async def cmd_xmpp_uptime(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the uptime of an XMPP server (XEP-0012).

    Usage:
        {prefix}xmpp uptime <domain>
        {prefix}x uptime <domain>
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    if not args or len(args) < 1:
        bot.reply(msg, "❌ Missing domain")
        return

    target = get_domain_from_jid(args[0])

    # Validate domain
    is_valid, error_msg = _validate_domain(target)
    if not is_valid:
        bot.reply(msg, f"❌ Invalid domain: {error_msg}")
        return

    if "@" in args[0]:
        bot.reply(msg, f"Note: 'uptime' only works with domains."
                       f" Using '{target}' from '{args[0]}'.")

    try:
        result = await bot.plugin["xep_0012"].get_last_activity(jid=target,
                                                                timeout=8)
        seconds = result['last_activity']['seconds']
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        uptime_str = []
        if days > 0:
            uptime_str.append(f"{days}d")
        if hours > 0:
            uptime_str.append(f"{hours}h")
        if minutes > 0:
            uptime_str.append(f"{minutes}m")
        if secs > 0 or not uptime_str:
            uptime_str.append(f"{secs}s")
        bot.reply(msg, f"⏱️ Uptime for {target}: {' '.join(uptime_str)}")
    except slixmpp.exceptions.IqTimeout:
        bot.reply(msg, f"🔴 Uptime request to {target} timed out.")
    except slixmpp.exceptions.IqError as e:
        err = e.iq['error']
        err_condition = err.get('condition', 'unknown')
        if err_condition == "service-unavailable":
            bot.reply(msg, f"🔴 {target} does not support uptime"
                           " requests (XEP-0012).")
        else:
            bot.reply(msg, f"🔴 Uptime request failed: {err_condition}")
    except Exception as e:
        bot.reply(msg, f"🔴 Error: {e}")


@command("xmpp items", role=Role.USER, aliases=["x items"])
async def cmd_xmpp_items(bot, sender_jid, nick, args, msg, is_room):
    """
    List the service items of an XMPP server (XEP-0030).

    Usage:
        {prefix}xmpp items <domain|jid>
        {prefix}x items <domain|jid>
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    target, error = _resolve_target(bot, args, msg, is_room, nick)
    if error:
        bot.reply(msg, f"❌ {error}")
        return
    target = inform_if_jid(msg, target, bot, "items")
    try:
        items = await bot.plugin["xep_0030"].get_items(jid=target, timeout=8)
        disco_items = items.get('disco_items', {})
        items_list = disco_items.get('items', [])
        if not items_list:
            bot.reply(msg, f"No items found for {target}")
            return
        formatted_items = []
        for item in items_list:
            if isinstance(item, tuple) and len(item) >= 1:
                jid = item[0]
                name = item[1] if len(item) > 1 else jid
                formatted_items.append(f"  • {jid} ({name})")
            else:
                formatted_items.append(f"  • {item}")
        result = f"📋 Items for {target}:\n" + "\n".join(formatted_items)
        bot.reply(msg, result)
    except slixmpp.exceptions.IqTimeout:
        bot.reply(msg, f"🔴 Items request to {target} timed out.")
    except slixmpp.exceptions.IqError as e:
        err = e.iq['error']
        err_condition = err.get('condition', 'unknown')
        if err_condition == "service-unavailable":
            bot.reply(msg, f"🔴 {target} does not support items"
                           " requests (XEP-0030).")
        else:
            bot.reply(msg, f"🔴 Items request failed: {err_condition}")
    except Exception as e:
        bot.reply(msg, f"🔴 Error: {e}")


@command("xmpp contact", role=Role.USER, aliases=["x contact"])
async def cmd_xmpp_contact(bot, sender_jid, nick, args, msg, is_room):
    """
    Display contact information for an XMPP server (XEP-0030).

    Usage:
        {prefix}xmpp contact <domain>
        {prefix}x contact <domain>
    """
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if _should_block_xmpp_contact(is_room, msg, enabled_rooms):
        return

    if not args:
        bot.reply(msg, "❌ Missing domain")
        return

    target = get_domain_from_jid(args[0])
    is_valid, error_msg = _validate_domain(target)
    if not is_valid:
        bot.reply(msg, f"❌ Invalid domain: {error_msg}")
        return

    _reply_xmpp_contact_domain_note(bot, msg, args[0], target)

    try:
        info = await bot.plugin["xep_0030"].get_info(jid=target, timeout=8)
        contact_info = _extract_xmpp_contact_info(info.get("disco_info", {}))
        _reply_xmpp_contact_result(bot, msg, target, contact_info)
    except slixmpp.exceptions.IqTimeout:
        bot.reply(msg, f"🔴 Contact request to {target} timed out.")
    except slixmpp.exceptions.IqError as e:
        _reply_xmpp_contact_iq_error(bot, msg, target, e)
    except Exception as e:
        bot.reply(msg, f"🔴 Error: {e}")


def _should_block_xmpp_contact(is_room, msg, enabled_rooms):
    return ((is_room or _is_muc_pm(msg))
            and msg["from"].bare not in enabled_rooms)


def _reply_xmpp_contact_domain_note(bot, msg, raw_target, target):
    if "@" in raw_target:
        bot.reply(
            msg,
            "Note: 'contact' only works with domains."
            f" Using '{target}' from '{raw_target}'."
        )


def _extract_xmpp_contact_info(disco_info):
    form = disco_info.get("form")
    if not form:
        return {}

    contact_info = {}
    for field in form:
        label = _contact_label_for_field(field.get("var", ""))
        values = field.get("value", [])
        if label and values:
            contact_info[label] = _normalize_contact_values(values)
    return contact_info


def _contact_label_for_field(field_var):
    lowered = field_var.lower()
    mapping = (
        ("admin", "Admin"),
        ("abuse", "Abuse"),
        ("security", "Security"),
        ("feedback", "Feedback"),
        ("support", "Support"),
    )
    for needle, label in mapping:
        if needle in lowered:
            return label
    return None


def _normalize_contact_values(values):
    return values if isinstance(values, list) else [values]


def _reply_xmpp_contact_result(bot, msg, target, contact_info):
    if not contact_info:
        bot.reply(
            msg,
            f"ℹ️  {target} does not provide contact"
            "information via XEP-0030"
        )
        return

    lines = _format_xmpp_contact_lines(contact_info)
    bot.reply(msg, f"📧 Contact info for {target}:\n" + "\n".join(lines))


def _format_xmpp_contact_lines(contact_info):
    contact_types = ["Admin", "Abuse", "Security", "Feedback", "Support"]
    lines = []
    for contact_type in contact_types:
        for addr in contact_info.get(contact_type, []):
            lines.append(f"  • {contact_type}: {addr}")
    return lines


def _reply_xmpp_contact_iq_error(bot, msg, target, exc):
    err_condition = _get_iq_error_condition(exc)
    if err_condition == "service-unavailable":
        bot.reply(
            msg,
            f"🔴 {target} does not support"
            " contact requests (XEP-0030)."
        )
    else:
        bot.reply(msg, f"🔴 Contact request failed: {err_condition}")


def _format_disco_identity(ident):
    if isinstance(ident, tuple) and len(ident) >= 2:
        category = ident[0]
        ident_type = ident[1]
        name = ident[2] if len(ident) > 2 else None
        ident_str = category
        if ident_type:
            ident_str += f"/{ident_type}"
        if name:
            ident_str += f" ({name})"
        return f"  • {ident_str}"
    return None


def _extract_xmpp_info_lines(disco_info):
    identities = []
    if 'identities' in disco_info:
        for ident in disco_info['identities']:
            formatted = _format_disco_identity(ident)
            if formatted:
                identities.append(formatted)

    features = []
    if 'features' in disco_info:
        features = [f"  • {feature}" for feature in disco_info['features']]

    return identities, features


def _build_xmpp_info_result(target, identities, features):
    result = f"🔍 Info for {target}:\n"
    if identities:
        result += "\n**Identities:**\n" + "\n".join(identities)
    if features:
        result += "\n**Features:**\n" + "\n".join(features[:10])
        if len(features) > 10:
            result += f"\n  ... and {len(features) - 10} more"
    if not identities and not features:
        result += "No identities or features found."
    return result


def _reply_xmpp_info_error(bot, msg, target, exc):
    if isinstance(exc, slixmpp.exceptions.IqTimeout):
        bot.reply(msg, f"🔴 Info request to {target} timed out.")
        return

    if isinstance(exc, slixmpp.exceptions.IqError):
        err = exc.iq['error']
        err_condition = err.get('condition', 'unknown')
        if err_condition == "service-unavailable":
            bot.reply(msg, f"🔴 {target} does not support"
                           " info requests (XEP-0030).")
        else:
            bot.reply(msg, f"🔴 Info request failed: {err_condition}")
        return

    bot.reply(msg, f"🔴 Error: {exc}")


@command("xmpp info", role=Role.USER, aliases=["x info"])
async def cmd_xmpp_info(bot, sender_jid, nick, args, msg, is_room):
    """
    List the identities and features of an XMPP server/domain (XEP-0030).

    Usage:
        {prefix}xmpp info <domain|jid>
        {prefix}x info <domain|jid>
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    target, error = _resolve_target(bot, args, msg, is_room, nick)
    if error:
        bot.reply(msg, f"❌ {error}")
        return

    # Always extract domain and notify if JID supplied
    target = inform_if_jid(msg, target, bot, "info")

    try:
        info = await bot.plugin["xep_0030"].get_info(jid=target, timeout=8)
        disco_info = info.get('disco_info', {})
        identities, features = _extract_xmpp_info_lines(disco_info)
        result = _build_xmpp_info_result(target, identities, features)
        bot.reply(msg, result)
    except Exception as e:
        _reply_xmpp_info_error(bot, msg, target, e)


@command("xmpp ping", role=Role.USER, aliases=["x ping"])
async def cmd_xmpp_ping(bot, sender_jid, nick, args, msg, is_room):
    """
    Ping an XMPP entity (JID or domain) and report round-trip time (XEP-0199).

    Usage:
        {prefix}xmpp ping <jid|domain>
        {prefix}x ping <jid|domain>
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    target, error = _resolve_target(bot, args, msg, is_room, nick)
    if error:
        bot.reply(msg, f"❌ {error}")
        return
    try:
        start = time.monotonic()
        await bot.plugin["xep_0199"].ping(jid=target, timeout=8)
        rtt = (time.monotonic() - start) * 1000
        bot.reply(msg, f"🏓 Pong from {target} in {rtt:.1f} ms")
    except slixmpp.exceptions.IqTimeout:
        bot.reply(msg, f"🔴 Ping to {target} timed out.")
    except slixmpp.exceptions.IqError as e:
        err = e.iq['error']
        err_type = err.get('type', 'unknown')
        err_condition = err.get('condition', 'unknown')
        err_text = err.get('text', '')
        bot.reply(
            msg,
            f"🔴 Ping to {target} failed: {err_type}/"
            f"{err_condition} {err_text}".strip()
        )
    except Exception as e:
        bot.reply(msg, f"🔴 Ping to {target} failed: {e}")


def _reply_xmpp_srv_missing_domain(bot, msg):
    bot.reply(msg, "❌ Missing domain\nUsage: {prefix}x srv <domain>")


def _reply_xmpp_srv_invalid_domain(bot, msg, error_msg):
    bot.reply(msg, f"❌ Invalid domain: {error_msg}")


def _reply_xmpp_srv_jid_notice(bot, msg, domain, original):
    bot.reply(msg, f"Note: 'srv' only works with domains."
                   f" Using '{domain}' from '{original}'.")


def _reply_xmpp_srv_dns_missing(bot, msg):
    bot.reply(msg, "🔴 DNS library not installed. Install"
                   " python-dnspython: pip install dnspython")


def _collect_srv_records(domain, service, dns_resolver, dns_exception):
    srv_name = f"{service}.{domain}"

    try:
        answers = dns_resolver.resolve(srv_name, 'SRV',
                                       raise_on_no_answer=False)

        if not answers:
            return "❌ Not found"

        records = []
        for rdata in answers:
            target = str(rdata.target).rstrip('.')
            port = rdata.port
            priority = rdata.priority
            weight = rdata.weight
            records.append({
                'target': target,
                'port': port,
                'priority': priority,
                'weight': weight
            })

        records.sort(key=lambda x: (x['priority'], -x['weight']))

        formatted = []
        for rec in records:
            formatted.append(
                f"{rec['target']}:{rec['port']} "
                f"(priority={rec['priority']}, weight={rec['weight']})"
            )

        return "\n    ".join(formatted)

    except dns_exception.DNSException as e:
        return f"❌ Not found ({type(e).__name__})"
    except Exception as e:
        return f"❌ Error: {e}"


def _build_xmpp_srv_result(domain, services, srv_records):
    result = f"🔍 DNS SRV records for **{domain}**:\n"
    found_any = False

    for service in services:
        status = srv_records[service]
        if "Not found" not in status and "Error" not in status:
            found_any = True
            result += f"\n**{service}:**\n    {status}"
        else:
            result += f"\n**{service}:** {status}"

    if not found_any:
        result += "\n\n⚠️ No SRV records found for this domain!"

    return result


@command("xmpp srv", role=Role.USER, aliases=["x srv"])
async def cmd_xmpp_srv(bot, sender_jid, nick, args, msg, is_room):
    """
    Perform DNS SRV lookups for XMPP services.

    Checks for:
    - _xmpp-client._tcp (Client-to-Server)
    - _xmpp-server._tcp (Server-to-Server)
    - _xmpps-client._tcp (XMPP over TLS)
    - _xmpps-server._tcp (XMPP-S Server)

    Usage:
        {prefix}xmpp srv <domain>
        {prefix}x srv <domain>

    Examples:
        {prefix}x srv example.com
        {prefix}x srv user@example.com    (uses example.com)
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    if not args or len(args) < 1:
        _reply_xmpp_srv_missing_domain(bot, msg)
        return

    domain = get_domain_from_jid(args[0])

    # Validate domain
    is_valid, error_msg = _validate_domain(domain)
    if not is_valid:
        _reply_xmpp_srv_invalid_domain(bot, msg, error_msg)
        return

    if "@" in args[0]:
        _reply_xmpp_srv_jid_notice(bot, msg, domain, args[0])

    try:
        import dns.resolver
        import dns.exception
    except ImportError:
        _reply_xmpp_srv_dns_missing(bot, msg)
        return

    try:
        services = [
            '_xmpp-client._tcp',
            '_xmpp-server._tcp',
            '_xmpps-client._tcp',
            '_xmpps-server._tcp',
        ]

        srv_records = {}
        for service in services:
            srv_records[service] = _collect_srv_records(
                domain,
                service,
                dns.resolver,
                dns.exception,
            )

        result = _build_xmpp_srv_result(domain, services, srv_records)
        bot.reply(msg, result)

    except Exception as e:
        bot.reply(msg, f"🔴 DNS lookup failed: {e}")


@command("xmpp compliance", role=Role.USER, aliases=["x compliance"])
async def cmd_xmpp_compliance(bot, sender_jid, nick, args, msg, is_room):
    """
    Show the compliance score of a server from compliance.conversations.im.

    Usage:
        {prefix}xmpp compliance <domain>
        {prefix}x compliance <domain>
    """
    # Check, if command is allowed in this context (room or MUC PM)
    store = await get_xmpp_store(bot)
    enabled_rooms = await store.get_global(XMPP_KEY, default={})
    if (is_room or _is_muc_pm(msg)) and msg["from"].bare not in enabled_rooms:
        return

    if not args or len(args) < 1:
        bot.reply(msg, "❌ Missing domain")
        return

    domain = get_domain_from_jid(args[0])

    # Validate domain
    is_valid, error_msg = _validate_domain(domain)
    if not is_valid:
        bot.reply(msg, f"❌ Invalid domain: {error_msg}")
        return

    if "@" in args[0]:
        bot.reply(msg, "Note: 'compliance' only works with domains. Using"
                       f"'{domain}' from '{args[0]}'.")

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://compliance.conversations.im/server/{domain}/"
            async with (
                    session.get(url,
                                timeout=aiohttp.ClientTimeout(total=8))
                    as resp):
                if resp.status == 200:
                    from bs4 import BeautifulSoup
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    score_elem = soup.find(class_='stat_result')
                    if score_elem:
                        score = score_elem.get_text(strip=True)
                        result_url = (f"https://compliance.conversations.im"
                                      f"/server/{domain}/")
                        bot.reply(msg, f"✅ Compliance score for {domain}:"
                                       f" **{score}**\nDetails: {result_url}")
                    else:
                        bot.reply(msg, "🔴 Could not extract compliance"
                                       f" score for {domain}")
                elif resp.status == 404:
                    bot.reply(msg, f"🔴 Server '{domain}' not found"
                                   " in compliance database")
                else:
                    bot.reply(msg, "🔴 Compliance database returned"
                                   f"status {resp.status}")
    except asyncio.TimeoutError:
        bot.reply(msg, "🔴 Compliance request timed out.")
    except aiohttp.ClientError as e:
        bot.reply(msg, f"🔴 Network error: {e}")
    except Exception as e:
        bot.reply(msg, f"🔴 Error: {e}")
