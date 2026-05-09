"""
Bot profile initialization plugin.

This plugin manages the public profile of the bot on the XMPP
network.

No Commands
-----------
This plugin has no commands, it is just run at startup or on reload
and sets the vCard and the Avatar if they've changed.

Responsibilities
----------------
• Publish or update the bot vCard (from vcard.py)
• Publish or update the bot avatar (XEP-0084)
• Do Avatar publishing using PEP (Personal Eventing Protocol) (XEP-0163)
• Avoid unnecessary updates using SHA1 hash comparison

If the configured avatar or vCard data has not changed since
the last run, the plugin skips the update to reduce network
traffic.

The profile setup is executed automatically when the XMPP
session starts or on plugin reload.
"""

import hashlib
import json
import logging
import os
import importlib.util

from slixmpp.xmlstream import ET
from utils.config import config

PLUGIN_META = {
    "name": "_reg_profile",
    "version": "0.3.0",
    "description": "Bot avatar and vCard profile management",
    "category": "core",
}

# Setup logging
log = logging.getLogger(__name__)

AVATAR_HASH_FILE = "avatar_hash.asc"
VCARD_HASH_FILE = "vcard_hash.asc"


# -------------------------------------------------
# HASH HELPERS
# -------------------------------------------------
def read_hash(path):
    """
    Read a previously stored SHA1 hash from a file.

    Parameters
    ----------
    path : str
        Path to the file containing the stored hash.

    Returns
    -------
    str or None
        The stored hash string if the file exists and can be read.
        Returns None if the file does not exist or reading fails.

    Notes
    -----
    Hash files are used to avoid unnecessary network updates
    for avatars and vCards. If the stored hash matches the newly
    calculated hash, the update will be skipped.
    """

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def write_hash(path, value):
    """
    Store a SHA1 hash value in a file.

    Parameters
    ----------
    path : str
        File path where the hash should be written.
    value : str
        SHA1 hash string to store.

    Notes
    -----
    This function is used after successfully publishing
    an avatar or updating a vCard. The stored hash allows
    the bot to detect whether the data has changed on
    subsequent startups.
    """

    try:
        with open(path, "w") as f:
            f.write(value)
    except Exception as e:
        log.error(f"[_REG_PROFILE] 🔴 Failed writing hash file {path}: {e}")


def sha1(data):
    """
    Compute the SHA1 hash of arbitrary binary data.

    Parameters
    ----------
    data : bytes
        Data for which the SHA1 digest should be calculated.

    Returns
    -------
    str
        Hexadecimal SHA1 digest string.

    Notes
    -----
    SHA1 hashes are used by the XMPP avatar specification
    and also serve as a lightweight method to detect whether
    avatar or vCard data has changed since the last run.
    """

    return hashlib.sha1(data).hexdigest()


# -------------------------------------------------
# VCARD BUILDER
# -------------------------------------------------
def build_vcard(card, data):
    """
    Recursively populate a vCard stanza from configuration data.

    Parameters
    ----------
    card : slixmpp.xmlstream.stanzabase.ElementBase
        The vCard stanza object obtained from the XEP-0054 plugin.
    data : dict
        Dictionary containing vCard fields from the configuration.

    Behavior
    --------
    - Iterates through all keys in the configuration dictionary.
    - If the value is another dictionary, a nested vCard element
      is created and populated recursively.
    - If the value is a scalar, it is assigned directly to the
      corresponding vCard field.
    """

    for key, value in data.items():

        if isinstance(value, dict):
            sub = card[key]
            build_vcard(sub, value)
        else:
            card[key] = value


# -------------------------------------------------
# VCARD UPDATE
# -------------------------------------------------
async def update_vcard(bot):
    """
    Update the XMPP vCard if plugins/vcard.py XML string has changed.
    Uses VCARD global from vcard.py (XML, as string).
    Skips update if hash matches.
    """
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    vcard_py_path = os.path.join(os.path.dirname(plugin_dir), "vcard.py")
    if not os.path.exists(vcard_py_path):
        log.warning("[_REG_PROFILE] vcard.py does not exist. Skipping vCard update.")
        return

    try:
        spec = importlib.util.spec_from_file_location("vcard", vcard_py_path)
        vcardmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vcardmod)
        VCARD = vcardmod.VCARD
        if not isinstance(VCARD, str):
            log.error("[_REG_PROFILE] VCARD variable in vcard.py is not a string!")
            return
    except Exception as e:
        log.error(f"[_REG_PROFILE] Error importing vcard.py: {e}")
        return

    # For hash comparison
    serialized = VCARD.encode("utf-8")
    new_hash = sha1(serialized)
    stored_hash = read_hash(VCARD_HASH_FILE)
    if stored_hash == new_hash:
        log.info("[_REG_PROFILE] vCard (from vcard.py/XML) unchanged — skipping update")
        return

    try:
        vcard_elem = ET.fromstring(VCARD)
        iq = bot.make_iq_set()
        iq.append(vcard_elem)
        await iq.send()
        write_hash(VCARD_HASH_FILE, new_hash)
        log.info("[_REG_PROFILE]✅ vCard (from vcard.py, XML string) updated")
    except Exception as e:
        log.error(f"[_REG_PROFILE]🔴 vCard upload from vcard.py (XML) failed: {e}")


# -------------------------------------------------
# AVATAR UPDATE
# -------------------------------------------------
async def update_avatar(bot):
    """
    Publish the bot's avatar using XMPP avatar protocols.

    Parameters
    ----------
    bot : Bot
        Instance of the Slixmpp-based bot.

    Process
    -------
    1. Load the avatar file defined in the configuration.
    2. Calculate the SHA1 hash of the image.
    3. Compare the hash with the stored avatar hash.
    4. If unchanged, skip publishing.
    5. Otherwise publish the avatar using XEP-0084.

    Avatar Requirements
    -------------------
    - Supported formats: PNG or JPEG
    - The image is read as binary data and sent directly
      through the Slixmpp XEP-0084 helper function.

    Notes
    -----
    The avatar is distributed via Personal Eventing Protocol
    (XEP-0163) so that clients subscribed to the user will
    automatically receive avatar updates.
    """

    avatar_path = config.get("avatar")
    avatar_type = config.get("avatar_type")

    if not avatar_path:
        return

    if not os.path.exists(avatar_path):
        log.warning("[_REG_PROFILE]🟡️ Avatar file not found")
        return

    try:

        with open(avatar_path, "rb") as f:
            avatar = f.read()

        new_hash = sha1(avatar)
        stored_hash = read_hash(AVATAR_HASH_FILE)

        if stored_hash == new_hash:
            log.info("[_REG_PROFILE] Avatar unchanged — skipping upload")
            return

        if avatar_type not in ("image/png", "image/jpeg"):
            log.error("[_REG_PROFILE]🔴 Avatar must be PNG or JPEG")
            return

        pubsub = bot["xep_0084"]

        await pubsub.publish_avatar(avatar)

        await pubsub.publish_avatar_metadata([
            {
                "id": new_hash,
                "type": f"{avatar_type}",
                "bytes": len(avatar)
            }
        ])

        write_hash(AVATAR_HASH_FILE, new_hash)

        log.info("[_REG_PROFILE]✅ Avatar updated")

    except Exception as e:
        log.error(f"[_REG_PROFILE]🔴 Avatar update failed: {e}")


# -------------------------------------------------
# MAIN SETUP
# -------------------------------------------------
async def setup_profile(bot):
    """
    Initialize the bot profile during session startup.

    Parameters
    ----------
    bot : Bot
        Instance of the Slixmpp bot client.

    Behavior
    --------
    This function performs all profile-related tasks once
    the XMPP session has started:

    1. Ensures the roster is available.
    2. Updates the vCard if necessary.
    3. Publishes a new avatar if it has changed.

    This function is triggered automatically by the
    ``session_start`` event handler registered by the plugin.
    """

    await bot.get_roster()

    try:
        um = bot.db.users
        if await um.get(str(bot.boundjid.bare)) is None:
            await um.create(str(bot.boundjid.bare), config.get("nick", None))
        log.info("[_REG_PROFILE]✅ user DB entry created or already exists")
    except Exception as e:
        log.error(f"['_REG_PROFILE]🔴 user DB entry creation failed: {e}")

    await update_vcard(bot)

    await update_avatar(bot)


# -------------------------------------------------
# REGISTER
# -------------------------------------------------
async def on_load(bot):
    """
    Register the profile plugin with the bot. That means it sets the the avatar
    and the vCard, if they've changed.

    Parameters
    ----------
    bot : Bot
        The main bot instance that loads this plugin.

    Behavior
    --------
    - Registers the required XMPP extensions for vcard-temp, PEP, User Avatar
    - Hooks the profile initialization into the
      ``session_start`` event.

    Registered Extensions
    ---------------------
    - XEP-0054 : vCard-temp
    - XEP-0163 : Personal Eventing Protocol
    - XEP-0084 : User Avatar
    """

    bot.register_plugin("xep_0054")
    bot.register_plugin("xep_0163")
    bot.register_plugin("xep_0084")

    await setup_profile(bot)


async def on_ready(bot):
    """
    Sets the timezone of the bot in the PluginRuntimeStore(GLOBAL)
    if the bot is fully set up.

    Parameters
    ----------
    bot : Bot
        The main bot instance.
    """
    # Set timezone on startup from config file
    store = bot.db.users.plugin("vcard")
    await store.set(str(bot.boundjid.bare), "TIMEZONE", config.get("timezone",
                                                                   "UTC"))
