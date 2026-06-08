"""XKCD Comic plugin.

Periodically checks for new XKCD comics and posts them to subscribed rooms.
Provides commands to view current, specific, random, and searched XKCD comics.

IMPORTANT: You may turn this plugin on for the current room with the command:
    {prefix}xkcd on

Commands:
• {prefix}xkcd - Show latest comic
• {prefix}xkcd <number> - Show specific XKCD comic
• {prefix}xkcd on/off/status - Enable/disable XKCD posting in this room
• {prefix}xkcd search <query> [page] - Search for XKCD by title/alt
• {prefix}xkcd random - Show a random XKCD comic
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import aiohttp

from plugins import _core
from plugins.rooms import JOINED_ROOMS
from utils.command import Role, command
from utils.config import config

log = logging.getLogger(__name__)

PLUGIN_META = {
    "name": "xkcd",
    "version": "1.2.0",
    "description": "XKCD comic fetcher and broadcaster with full indexing",
    "category": "fun",
    "requires": ["rooms", "_core"],
}

XKCD_KEY = "XKCD"
XKCD_LAST_ID_KEY = "XKCD_LAST_ID"
XKCD_INDEX_KEY = "XKCD_INDEX"

XKCD_API_URL = "https://xkcd.com/{}/info.0.json"
XKCD_LATEST_URL = "https://xkcd.com/info.0.json"
XKCD_COMIC_URL = "https://xkcd.com/{}"

CHECK_INTERVAL = 3600
INDEX_START_DELAY_SECONDS = 30
INDEX_REQUEST_DELAY_SECONDS = 0.15

CHECK_TASK: asyncio.Task | None = None
INDEX_TASK: asyncio.Task | None = None
LAST_COMIC_ID = 0

# XKCD comic #404 intentionally does not exist.
MISSING_COMIC_IDS = {404}


async def get_xkcd_store(bot):
    """Get the database store for XKCD settings."""
    return bot.db.users.plugin("xkcd")


async def fetch_xkcd(url: str, session: aiohttp.ClientSession | None = None):
    """Fetch XKCD comic info from API."""
    try:
        if session is not None:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.debug("[XKCD] Non-200 response for %s: %s",
                          url, resp.status)
                return None

        async with aiohttp.ClientSession() as own_session:
            async with own_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.debug("[XKCD] Non-200 response for %s: %s",
                          url, resp.status)
                return None

    except Exception as exc:
        log.warning("[XKCD] Failed to fetch %s: %s", url, exc)
        return None


async def get_latest_xkcd(session: aiohttp.ClientSession | None = None):
    """Fetch the latest XKCD comic."""
    return await fetch_xkcd(XKCD_LATEST_URL, session=session)


async def get_xkcd(comic_id: int,
                   session: aiohttp.ClientSession | None = None):
    """Fetch a specific XKCD comic by ID."""
    return await fetch_xkcd(XKCD_API_URL.format(comic_id), session=session)


def format_comic_message(comic: dict[str, Any]) -> str:
    """Format XKCD comic info text."""
    num = comic.get("num", "?")
    title = comic.get("title", "No title")
    alt = comic.get("alt", "")

    msg = f"🎨 XKCD #{num}: {title}\n"
    if alt:
        msg += f"💬 {alt}\n"
    msg += f"🔗 {XKCD_COMIC_URL.format(num)}"
    return msg


def normalize_image_url(url: str | None) -> str | None:
    """Ensure image URL has a usable scheme."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://imgs.xkcd.com" + url
    return url


async def send_url_with_oob(bot, target, url: str, mtype: str):
    """Send a URL in the message body and include XEP-0066 OOB data."""
    message = bot.make_message(
        mto=target,
        mbody=url,
        mtype=mtype,
    )

    try:
        message["oob"]["url"] = url
    except Exception as exc:
        log.debug("[XKCD] Could not attach XEP-0066 OOB data: %s", exc)

    message.send()


async def send_xkcd_room(bot, room_id: str, comic: dict[str, Any] | None):
    """Send XKCD comic to a room."""
    if not comic:
        return

    try:
        img_url = normalize_image_url(comic.get("img"))
        if not img_url:
            log.warning("[XKCD] No image URL in comic %s", comic.get("num"))
            return

        info_msg = format_comic_message(comic)
        bot.reply(
            {
                "from": type("F", (), {"bare": room_id})(),
                "type": "groupchat",
            },
            info_msg,
            mention=False,
            thread=True,
            rate_limit=False,
            ephemeral=False,
        )

        # Ensure the info message lands before the image URL/OOB message.
        await asyncio.sleep(0.2)

        log.debug(
            "[XKCD] Sending comic #%s to room %s via direct URL + OOB",
            comic.get("num"),
            room_id,
        )
        await send_url_with_oob(bot, room_id, img_url, "groupchat")
        log.debug("[XKCD] ✅ Comic #%s sent to room", comic.get("num"))

    except Exception as exc:
        log.exception(
            "[XKCD] Failed to send comic %s to room %s: %s",
            comic.get("num"),
            room_id,
            exc,
        )


async def send_xkcd_dm(bot, target_jid: str, comic: dict[str, Any] | None):
    """Send XKCD comic via DM, including MUC PM, with XEP-0066 OOB."""
    if not comic:
        return

    try:
        img_url = normalize_image_url(comic.get("img"))
        if not img_url:
            log.warning("[XKCD] No image URL in comic %s", comic.get("num"))
            return

        info_msg = format_comic_message(comic)
        message = bot.make_message(
            mto=target_jid,
            mbody=info_msg,
            mtype="chat",
        )
        message.send()

        # Ensure the info message lands before the image URL/OOB message.
        await asyncio.sleep(0.2)

        log.debug(
            "[XKCD] Sending comic #%s to DM %s via direct URL + OOB",
            comic.get("num"),
            target_jid,
        )
        await send_url_with_oob(bot, target_jid, img_url, "chat")
        log.debug("[XKCD] ✅ Comic #%s sent to DM", comic.get("num"))

    except Exception as exc:
        log.exception(
            "[XKCD] Failed to send comic %s to DM %s: %s",
            comic.get("num"),
            target_jid,
            exc,
        )


async def get_last_comic_id(bot) -> int:
    """Get last posted comic ID from database."""
    store = await get_xkcd_store(bot)
    data = await store.get_global(XKCD_LAST_ID_KEY, default={"id": 0})
    if not isinstance(data, dict):
        return 0
    return int(data.get("id", 0) or 0)


async def save_last_comic_id(bot, comic_id: int):
    """Save last posted comic ID to database."""
    store = await get_xkcd_store(bot)
    await store.set_global(XKCD_LAST_ID_KEY, {"id": comic_id})
    log.debug("[XKCD] Saved last comic ID to DB: %s", comic_id)


async def add_comic_to_index(bot, comic: dict[str, Any] | None):
    """Add a comic to the search index."""
    if not comic:
        return

    comic_id = comic.get("num")
    if not comic_id:
        return

    store = await get_xkcd_store(bot)
    search_index = await store.get_global(XKCD_INDEX_KEY, default={})
    if not isinstance(search_index, dict):
        search_index = {}

    search_index[str(comic_id)] = {
        "title": comic.get("title", ""),
        "alt": comic.get("alt", ""),
    }
    await store.set_global(XKCD_INDEX_KEY, search_index)


async def get_subscribed_rooms(bot) -> list[str]:
    """Return subscribed rooms.

    Supports both legacy list storage:
        {"rooms": ["room@conference.example"]}

    and new dict storage:
        {"room@conference.example": True}
    """
    store = await get_xkcd_store(bot)
    state = await store.get_global(XKCD_KEY, default={})

    if not isinstance(state, dict):
        return []

    # Legacy format.
    rooms = state.get("rooms")
    if isinstance(rooms, list):
        return [str(room) for room in rooms if room]

    # New format.
    return [str(room) for room, enabled in state.items() if enabled is True]


async def migrate_xkcd_room_storage(bot):
    """Migrate legacy {'rooms': [...]} storage to {room_jid: True}."""
    store = await get_xkcd_store(bot)
    state = await store.get_global(XKCD_KEY, default={})

    if not isinstance(state, dict):
        return

    rooms = state.get("rooms")
    if not isinstance(rooms, list):
        return

    migrated = {str(room): True for room in rooms if room}
    await store.set_global(XKCD_KEY, migrated)

    log.info("[XKCD] Migrated %s subscribed rooms to dict storage",
             len(migrated))


async def broadcast_comic_to_subscribed_rooms(bot, comic: dict[str, Any]):
    """Broadcast a comic to all subscribed rooms."""
    rooms = await get_subscribed_rooms(bot)
    log.info("[XKCD] Broadcasting comic #%s to %s rooms",
             comic.get("num"), len(rooms))

    for room_id in rooms:
        if room_id in JOINED_ROOMS:
            try:
                await send_xkcd_room(bot, room_id, comic)
                await asyncio.sleep(0.5)
            except Exception as exc:
                log.exception(
                    "[XKCD] Error sending comic #%s to %s: %s",
                    comic.get("num"),
                    room_id,
                    exc,
                )
        else:
            log.warning("[XKCD] Room %s not in JOINED_ROOMS", room_id)


async def _persist_xkcd_index(store, search_index):
    await store.set_global(XKCD_INDEX_KEY, search_index)


def _normalize_xkcd_index(search_index):
    if not isinstance(search_index, dict):
        return {}
    return search_index


def _expected_xkcd_count(current_max_id):
    return current_max_id - len(
        {comic_id for comic_id in MISSING_COMIC_IDS
            if comic_id <= current_max_id}
    )


def _xkcd_should_skip_comic(comic_id, search_index):
    return comic_id in MISSING_COMIC_IDS or str(comic_id) in search_index


async def _index_single_xkcd_comic(
    comic_id,
    session,
    store,
    search_index,
    indexed,
    failed,
):
    try:
        comic = await get_xkcd(comic_id, session=session)
        if comic:
            search_index[str(comic_id)] = {
                "title": comic.get("title", ""),
                "alt": comic.get("alt", ""),
            }
            indexed += 1

            if indexed % 200 == 0:
                await _persist_xkcd_index(store, search_index)
                log.info("[XKCD] Indexed %s new comics...", indexed)

            await asyncio.sleep(INDEX_REQUEST_DELAY_SECONDS)
        else:
            failed += 1
            log.debug("[XKCD] Comic #%s could not be fetched", comic_id)

    except asyncio.CancelledError:
        log.info("[XKCD] Index building cancelled after %s new comics",
                 indexed)
        await _persist_xkcd_index(store, search_index)
        raise

    except Exception as exc:
        log.debug("[XKCD] Failed to index comic #%s: %s", comic_id, exc)
        failed += 1

    return indexed, failed


async def build_full_index(bot):
    """Build full search index of all XKCD comics."""
    try:
        await asyncio.sleep(INDEX_START_DELAY_SECONDS)

        async with aiohttp.ClientSession() as session:
            latest = await get_latest_xkcd(session=session)
            if not latest:
                log.warning("[XKCD] Could not fetch latest comic for indexing")
                return

            store = await get_xkcd_store(bot)
            search_index = _normalize_xkcd_index(
                await store.get_global(XKCD_INDEX_KEY, default={})
            )

            current_max_id = int(latest.get("num", 0) or 0)
            expected_count = _expected_xkcd_count(current_max_id)

            if search_index and len(search_index) >= expected_count:
                log.info(
                    "[XKCD] Search index up to date with %s entries",
                    len(search_index),
                )
                return

            log.info(
                "[XKCD] Building full search index up to comic #%s "
                "(currently have %s/%s)...",
                current_max_id,
                len(search_index),
                expected_count,
            )

            indexed = 0
            failed = 0

            for comic_id in range(1, current_max_id + 1):
                if _xkcd_should_skip_comic(comic_id, search_index):
                    continue

                indexed, failed = await _index_single_xkcd_comic(
                    comic_id,
                    session,
                    store,
                    search_index,
                    indexed,
                    failed,
                )

            await _persist_xkcd_index(store, search_index)
            log.info(
                "[XKCD] ✅ Search index complete! Added %s comics (%s failed)",
                indexed,
                failed,
            )

    except asyncio.CancelledError:
        log.debug("[XKCD] Index task cancelled")
        raise

    except Exception as exc:
        log.exception("[XKCD] Error building search index: %s", exc)


async def catch_up_missing_comics(bot, start_id: int, end_id: int):
    """Fetch, index, and broadcast comics from start_id to end_id inclusive."""
    global LAST_COMIC_ID

    if end_id < start_id:
        return

    log.info("[XKCD] ⏳ Catching up from #%s to #%s", start_id, end_id)

    async with aiohttp.ClientSession() as session:
        for comic_id in range(start_id, end_id + 1):
            if comic_id in MISSING_COMIC_IDS:
                LAST_COMIC_ID = comic_id
                await save_last_comic_id(bot, comic_id)
                continue

            comic = await get_xkcd(comic_id, session=session)
            if not comic:
                log.warning("[XKCD] Could not fetch comic #%s", comic_id)
                continue

            await add_comic_to_index(bot, comic)
            await broadcast_comic_to_subscribed_rooms(bot, comic)

            LAST_COMIC_ID = comic_id
            await save_last_comic_id(bot, comic_id)


async def xkcd_check_loop(bot):
    """Periodically check for new XKCD comics and broadcast them."""
    global LAST_COMIC_ID

    try:
        latest = await get_latest_xkcd()
        if not latest:
            log.error("[XKCD] Could not fetch initial comic info")
            return

        LAST_COMIC_ID = await get_last_comic_id(bot)
        current_id = int(latest.get("num", 0) or 0)

        if LAST_COMIC_ID == 0:
            LAST_COMIC_ID = current_id
            await save_last_comic_id(bot, current_id)
            log.info("[XKCD] First run: Initialized from comic #%s",
                     current_id)
        elif current_id > LAST_COMIC_ID:
            await catch_up_missing_comics(bot, LAST_COMIC_ID + 1, current_id)
        else:
            log.debug("[XKCD] Polling started: No new comics (last=%s)",
                      LAST_COMIC_ID)

        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL)

                latest = await get_latest_xkcd()
                if not latest:
                    log.warning("[XKCD] Failed to fetch latest comic")
                    continue

                current_id = int(latest.get("num", 0) or 0)
                log.debug(
                    "[XKCD] Poll check: last=%s, current=%s",
                    LAST_COMIC_ID,
                    current_id,
                )

                if current_id > LAST_COMIC_ID:
                    await catch_up_missing_comics(bot, LAST_COMIC_ID + 1,
                                                  current_id)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                log.exception("[XKCD] Error in check loop: %s", exc)

    except asyncio.CancelledError:
        log.debug("[XKCD] Check loop cancelled")
        raise


@command("xkcd", role=Role.USER)
async def xkcd_command(bot, sender_jid, nick, args, msg, is_room):
    """Manage and view XKCD comics."""
    from_jid = str(msg["from"].bare)
    target_jid = str(msg["from"])
    is_muc_pm = from_jid in JOINED_ROOMS

    log.debug(
        "[XKCD] Command: args=%s, is_room=%s, is_muc_pm=%s, from_jid=%s",
        args,
        is_room,
        is_muc_pm,
        from_jid,
    )

    command_prefix = config.get("prefix", ",")
    lowered_args = [str(arg).lower() for arg in args]

    if await _core.handle_room_toggle_command(
        bot,
        msg,
        is_room,
        lowered_args,
        store_getter=get_xkcd_store,
        key=XKCD_KEY,
        label="XKCD posting",
        storage="dict",
        log_prefix="[XKCD]",
    ):
        return

    if await _block_muc_pm_when_disabled(
        bot,
        msg,
        is_muc_pm,
        from_jid,
        command_prefix,
    ):
        return

    if lowered_args and lowered_args[0] == "search":
        await _handle_xkcd_search(bot, msg, args, lowered_args, command_prefix)
        return

    if lowered_args and lowered_args[0] == "random":
        await _handle_xkcd_random(bot, msg, from_jid, target_jid, is_room)
        return

    if args:
        handled = await _handle_specific_xkcd(bot, msg, args,
                                              from_jid, target_jid, is_room)
        if handled:
            return

    await _handle_latest_xkcd(bot, msg, from_jid, target_jid, is_room)


async def _block_muc_pm_when_disabled(bot, msg, is_muc_pm,
                                      from_jid, command_prefix):
    if not is_muc_pm:
        return False

    rooms = await get_subscribed_rooms(bot)
    if from_jid in rooms:
        return False

    bot.reply(
        msg,
        "ℹ️ XKCD is not enabled in this room.\n"
        f"Use '{command_prefix}xkcd on' in a MUC DM to enable it.",
    )
    log.info("[XKCD] Command blocked: XKCD not enabled in %s", from_jid)
    return True


async def _handle_xkcd_search(bot, msg, args, lowered_args, command_prefix):
    if len(args) < 2:
        bot.reply(msg, f"❌ Usage: {command_prefix}xkcd search <query> [page]")
        return

    page, query = _parse_xkcd_search_args(args)
    if not query:
        bot.reply(msg, f"❌ Usage: {command_prefix}xkcd search <query> [page]")
        return

    if page < 1:
        bot.reply(msg, "❌ Page number must be 1 or greater.")
        return

    log.debug("[XKCD] Searching for: %s (page %s)", query, page)

    store = await get_xkcd_store(bot)
    search_index = await store.get_global(XKCD_INDEX_KEY, default={})
    if not isinstance(search_index, dict) or not search_index:
        bot.reply(
            msg,
            "❌ Search index not built.\nPlease wait forindexing to complete.",
        )
        return

    results = _search_xkcd_index(search_index, query)
    if not results:
        bot.reply(msg, f"❌ No XKCDs found matching '{query}'")
        return

    results.sort(key=lambda item: item["id"], reverse=True)

    per_page = 10
    page_results, page, total_pages, total_results = _core.paginate_items(
        results,
        page,
        per_page,
    )

    msg_lines = [
        f"🔎 Found {total_results} results for '{query}'"
        f" (page {page}/{total_pages}):"
    ]

    start_index = (page - 1) * per_page
    for i, result in enumerate(page_results, start_index + 1):
        msg_lines.append(f"{i}. #{result['id']}: {result['title']}")
        if result["alt"]:
            msg_lines.append(f"   Alt: {_truncate_alt_text(result['alt'])}")

    if page < total_pages:
        msg_lines.append(f"\n➡️ Next page: {command_prefix}xkcd"
                         f" search {query} {page + 1}")
    if page > 1:
        msg_lines.append(f"⬅️ Previous page: {command_prefix}xkcd"
                         f" search {query} {page - 1}")

    bot.reply(msg, "\n".join(msg_lines))


def _parse_xkcd_search_args(args):
    page = 1
    if len(args) >= 3 and str(args[-1]).isdigit():
        page = int(args[-1])
        query = " ".join(str(arg) for arg in args[1:-1]).lower()
    else:
        query = " ".join(str(arg) for arg in args[1:]).lower()
    return page, query


def _search_xkcd_index(search_index, query):
    results = []
    for comic_id_str, comic_data in search_index.items():
        if not isinstance(comic_data, dict):
            continue

        title = comic_data.get("title", "").lower()
        alt = comic_data.get("alt", "").lower()

        if query in title or query in alt:
            try:
                comic_id = int(comic_id_str)
            except ValueError:
                continue

            results.append(
                {
                    "id": comic_id,
                    "title": comic_data.get("title", ""),
                    "alt": comic_data.get("alt", ""),
                }
            )
    return results


def _truncate_alt_text(alt_text):
    if len(alt_text) > 80:
        return alt_text[:80] + "..."
    return alt_text[:80]


async def _handle_xkcd_random(bot, msg, from_jid, target_jid, is_room):
    latest = await get_latest_xkcd()
    if not latest:
        bot.reply(msg, "❌ Failed to fetch XKCD data.")
        return

    max_id = int(latest.get("num", 1) or 1)

    random_id = _pick_valid_random_xkcd_id(max_id)
    if random_id is None:
        bot.reply(msg, "❌ Failed to pick a valid random XKCD.")
        return

    comic = await get_xkcd(random_id)
    if comic:
        if is_room:
            await send_xkcd_room(bot, from_jid, comic)
        else:
            await send_xkcd_dm(bot, target_jid, comic)
    else:
        bot.reply(msg, f"❌ Failed to fetch XKCD #{random_id}.")


def _pick_valid_random_xkcd_id(max_id):
    for _ in range(20):
        random_id = random.randint(1, max_id)
        if random_id not in MISSING_COMIC_IDS:
            return random_id
    return None


async def _handle_specific_xkcd(bot, msg, args, from_jid, target_jid, is_room):
    try:
        comic_id = int(args[0])
    except ValueError:
        return False

    if comic_id in MISSING_COMIC_IDS:
        bot.reply(msg, f"❌ XKCD #{comic_id} does not exist.")
        return True

    if comic_id < 1:
        bot.reply(msg, "❌ XKCD number must be 1 or greater.")
        return True

    comic = await get_xkcd(comic_id)
    if comic:
        if is_room:
            await send_xkcd_room(bot, from_jid, comic)
        else:
            await send_xkcd_dm(bot, target_jid, comic)
    else:
        bot.reply(msg, f"❌ XKCD #{comic_id} not found.")
    return True


async def _handle_latest_xkcd(bot, msg, from_jid, target_jid, is_room):
    latest = await get_latest_xkcd()
    if latest:
        if is_room:
            await send_xkcd_room(bot, from_jid, latest)
        else:
            await send_xkcd_dm(bot, target_jid, latest)
    else:
        bot.reply(msg, "❌ Failed to fetch latest XKCD.")


async def _cancel_task(task: asyncio.Task | None, name: str):
    """Cancel a background task safely."""
    if not task:
        return

    if task.done():
        return

    log.debug("[XKCD] Cancelling %s task...", name)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        log.debug("[XKCD] %s task cancelled", name)
    except Exception as exc:
        log.exception("[XKCD] Error while cancelling %s task: %s", name, exc)


async def on_load(bot):
    """Load the XKCD plugin and start background tasks safely."""
    global CHECK_TASK, INDEX_TASK

    log.info("[XKCD] Plugin loading...")

    # Register XEP-0066 if available.
    try:
        if not bot.plugin.get("xep_0066", None):
            bot.register_plugin("xep_0066")
            log.info("[XKCD] XEP-0066 (Out of Band Data) registered")
    except Exception as exc:
        log.debug("[XKCD] Could not register XEP-0066: %s", exc)

    try:
        await migrate_xkcd_room_storage(bot)
    except Exception as exc:
        log.exception("[XKCD] Failed to migrate room storage: %s", exc)

    # Avoid duplicate tasks on plugin reload.
    await _cancel_task(INDEX_TASK, "index")
    await _cancel_task(CHECK_TASK, "check")

    INDEX_TASK = None
    CHECK_TASK = None

    INDEX_TASK = asyncio.create_task(
        build_full_index(bot),
        name="xkcd-index",
    )
    CHECK_TASK = asyncio.create_task(
        xkcd_check_loop(bot),
        name="xkcd-check",
    )

    log.info("[XKCD] Plugin loaded, background tasks started")


async def on_unload(bot):
    """Unload the XKCD plugin and stop background tasks."""
    global CHECK_TASK, INDEX_TASK

    log.info("[XKCD] Plugin unloading...")

    await _cancel_task(INDEX_TASK, "index")
    await _cancel_task(CHECK_TASK, "check")

    INDEX_TASK = None
    CHECK_TASK = None

    log.info("[XKCD] Plugin unloaded")
