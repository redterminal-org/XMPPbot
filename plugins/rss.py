"""
RSS Feed watcher plugin.

Periodically checks configured RSS/Atom feeds every 20 minutes. You can
add/delete specified feeds to your room.

Commands:
    {prefix}rss add <url>
    {prefix}rss delete <url>
    {prefix}rss list

Feed configuration is stored in the plugin runtime store under the key "RSS".
"""

import asyncio
import logging
import time
import html
import hashlib
from difflib import SequenceMatcher
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils.command import command, Role
from utils.config import config
from plugins.rooms import JOINED_ROOMS

try:
    import feedparser
except ImportError:
    feedparser = None

PLUGIN_META = {
    "name": "rss",
    "version": "0.2.1",
    "description": "RSS/Atom feed watcher and poster",
    "category": "info",
    "requires": ["rooms"],
}

log = logging.getLogger(__name__)

RSS_KEY = "RSS"
CHECK_TASKS = {}

# Configuration constants
DEFAULT_POLL_INTERVAL = 1200  # 20 minutes
BACKOFF_INCREMENT_MULTIPLIER = 60  # seconds per error
MAX_BACKOFF_TIME = 86400  # 24 hours
SIMILARITY_THRESHOLD = 0.8  # 80% similarity = duplicate


def html_to_text_with_links(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            a.replace_with(f"{a.get_text()} ({href})")
    text = soup.get_text(separator=" ", strip=True)
    return html.unescape(text)


def _should_include_description(title: str, description: str, similarity_threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """
    Intelligently check if description should be included.

    Returns False if:
    - Description is empty
    - Description equals title (exact match)
    - Description starts with title (truncated title case)
    - Similarity is above threshold (fuzzy match)
    - Title starts with description (inverse case)

    Args:
        title: Entry title
        description: Entry description
        similarity_threshold: Similarity score (0-1) above which they're considered duplicates

    Returns:
        True if description is meaningfully different, False otherwise
    """
    if not description:
        return False

    # Exact match
    if description == title:
        return False

    # Normalize both for comparison (lowercase, strip whitespace)
    title_norm = title.lower().strip()
    desc_norm = description.lower().strip()

    # One is substring of the other (handles truncation cases)
    if title_norm in desc_norm or desc_norm in title_norm:
        return False

    # Fuzzy similarity check
    similarity = SequenceMatcher(None, title_norm, desc_norm).ratio()
    if similarity >= similarity_threshold:
        return False

    return True


def _extract_entry_link(entry) -> str:
    """
    Extract the best link from an entry following feed standards.

    For Atom feeds: Check entry.links with rel="alternate"
    For JSON Feed: Check entry.url
    Fallback: entry.id (if it's a URL)

    Args:
        entry: Parsed feed entry

    Returns:
        Best available link URL or empty string
    """

    # Atom standard: entry.links with rel="alternate"
    if "links" in entry and isinstance(entry.links, list):
        for link_obj in entry.links:
            if isinstance(link_obj, dict):
                if link_obj.get("rel") in (None, "alternate"):  # None = default rel
                    href = link_obj.get("href")
                    if href and isinstance(href, str) and href.startswith(("http://", "https://")):
                        return href.strip()

    # Standard entry.link
    entry_link = entry.get("link")
    if entry_link and isinstance(entry_link, str) and entry_link.startswith(("http://", "https://")):
        return entry_link.strip()

    # JSON Feed standard: entry.url
    entry_url = entry.get("url")
    if entry_url and isinstance(entry_url, str) and entry_url.startswith(("http://", "https://")):
        return entry_url.strip()

    # Fallback: entry.id (if it's a URL)
    # Note: entry.id is primarily for identification, not necessarily a URL
    entry_id = entry.get("id")
    if entry_id and isinstance(entry_id, str) and entry_id.startswith(("http://", "https://")):
        return entry_id.strip()

    return ""


def _generate_entry_id(title: str, description: str, link: str) -> str:
    """
    Generate stable entry ID with multiple fallbacks.

    Priority:
    1. Use link if available (most reliable)
    2. Hash title+description if no link

    Args:
        title: Entry title
        description: Entry description
        link: Entry link/URL

    Returns:
        Stable entry ID string
    """
    if link and link.strip():
        return link

    # Hash title+description for unique IDs when no link available
    combined = f"{title}|{description}".encode('utf-8')
    return hashlib.sha256(combined).hexdigest()


def _normalize_url(url: str) -> str:
    """
    Normalize URL for consistent storage and comparison.

    Args:
        url: URL to normalize

    Returns:
        Normalized URL
    """
    if not url:
        return url

    # Remove trailing slashes and normalize scheme
    url = url.rstrip('/')

    # Ensure scheme exists
    if not url.startswith(('http://', 'https://', 'ftp://')):
        url = 'https://' + url

    return url


def _resolve_relative_url(base_url: str, relative_url: str) -> str:
    """
    Resolve relative URLs against base URL.

    Args:
        base_url: Base URL (feed URL or feed link)
        relative_url: URL that may be relative

    Returns:
        Absolute URL
    """
    if not relative_url:
        return relative_url

    # Already absolute?
    if relative_url.startswith(('http://', 'https://', 'ftp://', 'mailto:')):
        return relative_url

    if not base_url:
        return relative_url

    try:
        return urljoin(base_url, relative_url)
    except Exception as e:
        log.warning(f"Failed to resolve relative URL {relative_url} against {base_url}: {e}")
        return relative_url


def _get_feed_headers() -> dict[str, str]:
    """Get HTTP headers for feed requests."""
    return {
        "User-Agent": "envsbot/1.0 +https://github.com/envs/envsbot",
        "Accept": "application/rss+xml, application/atom+xml, application/json, */*",
    }


def _now():
    return int(time.time())


async def get_feeds(store):
    feeds = await store.get_global(RSS_KEY, default={})
    return feeds if isinstance(feeds, dict) else {}


async def save_feeds(store, feeds):
    await store.set_global(RSS_KEY, feeds)


async def fetch_feed(url):
    """
    Fetch and parse RSS feed with proper URL handling.

    Prevents feedparser from modifying the feed URL through redirects or normalization.

    Args:
        url: Feed URL to fetch

    Returns:
        Parsed feed result
    """
    if not feedparser:
        raise RuntimeError("feedparser module not installed")

    headers = _get_feed_headers()

    # Parse with request_headers and preserve original URL
    result = await asyncio.to_thread(
        feedparser.parse,
        url,
        request_headers=headers,
    )

    # Force the feed URL to be the original URL we requested
    # This prevents feedparser from using redirected URLs
    if 'feed' in result:
        result.feed['href'] = url
        result.feed['id'] = url

    return result


async def rss_check_loop(bot, store, url, period):
    """Periodically check a feed for updates and post new items."""
    while True:
        feeds = await get_feeds(store)
        # Exit loop if feed has been deleted
        if url not in feeds:
            break

        feed = feeds[url]
        feed_title = feed["title"]
        feed_link = feed.get("link", url)  # Use feed link for relative URL resolution
        last_id = feed.get("last_id")
        rooms = feed.get("rooms", [])
        error_count = feed.get("error_count", 0)
        next_retry = feed.get("next_retry", 0)

        # Check if we should retry based on backoff
        now = _now()
        if next_retry > now:
            await asyncio.sleep(min(period, next_retry - now))
            continue

        try:
            parsed = await fetch_feed(url)
        except Exception as e:
            log.warning(f"Failed to fetch RSS feed {url}: {e}")
            # Apply exponential backoff
            error_count += 1
            backoff_delay = DEFAULT_POLL_INTERVAL * BACKOFF_INCREMENT_MULTIPLIER * error_count
            backoff_delay = min(backoff_delay, MAX_BACKOFF_TIME)
            next_retry = now + backoff_delay
            feeds = await get_feeds(store)
            if url in feeds:
                feeds[url]["error_count"] = error_count
                feeds[url]["next_retry"] = next_retry
                await save_feeds(store, feeds)
                log.debug(f"Feed {url} backoff set to {error_count} errors, retry at {next_retry}")
            await asyncio.sleep(period)
            continue

        if not parsed.entries:
            log.debug(f"Feed {url} has no entries")
            await asyncio.sleep(period)
            continue

        # Reset error count on successful fetch
        if error_count > 0:
            log.debug(f"Feed {url} recovered, resetting error count")
            feeds = await get_feeds(store)
            if url in feeds:
                feeds[url]["error_count"] = 0
                feeds[url]["next_retry"] = 0
                await save_feeds(store, feeds)

        # Update feed link for URL resolution if available
        if 'feed' in parsed and 'link' in parsed.feed:
            feed_link = parsed.feed['link']
            feeds = await get_feeds(store)
            if url in feeds:
                feeds[url]["link"] = feed_link
                await save_feeds(store, feeds)

        # Find new entries
        new_entries = []
        for entry in parsed.entries:
            entry_link = _extract_entry_link(entry)
            entry_id = _generate_entry_id(
                entry.get("title", ""),
                entry.get("description", ""),
                entry_link
            )
            if not entry_id:
                continue
            if last_id == entry_id:
                break
            new_entries.append(entry)

        # Post new entries in reverse order (oldest first)
        last_saved_id = None
        for entry in reversed(new_entries):
            entry_link = _extract_entry_link(entry)
            entry_id = _generate_entry_id(
                entry.get("title", ""),
                entry.get("description", ""),
                entry_link
            )
            entry_title = html_to_text_with_links(
                entry.get("title", "No title")
            )
            entry_desc = html_to_text_with_links(entry.get("description", ""))

            # Resolve relative URLs
            entry_link = _resolve_relative_url(feed_link, entry_link)
            entry_link = _normalize_url(entry_link)

            # Only add description if it's meaningfully different from title
            if _should_include_description(entry_title, entry_desc):
                msg = f"[RSS] ({feed_title}) {entry_title} - {entry_desc}\n"
            else:
                msg = f"[RSS] ({feed_title}) {entry_title}\n"
            msg += f"{entry_link}"
            for room in rooms:
                if room in JOINED_ROOMS:
                    bot.reply(
                        {
                            "from": type(
                                "F", (), {"bare": room}
                            )(),
                            "type": "groupchat",
                        },
                        msg,
                        mention=False,
                        thread=True,
                        rate_limit=False,
                        ephemeral=False,
                    )

            # Save last_id IMMEDIATELY after each post
            last_saved_id = entry_id
            feeds = await get_feeds(store)
            if url not in feeds:
                log.warning(f"Feed {url} was deleted during posting!")
                break
            feeds[url]["last_id"] = last_saved_id
            await save_feeds(store, feeds)

        await asyncio.sleep(period)


async def ensure_task(bot, store, url, period):
    """Ensure a check task is running for the given feed."""
    if url in CHECK_TASKS and not CHECK_TASKS[url].done():
        return
    CHECK_TASKS[url] = asyncio.create_task(
        rss_check_loop(bot, store, url, period)
    )


async def restart_all_tasks(bot):
    store = bot.db.users.plugin("rss")
    feeds = await get_feeds(store)
    for url, feed in feeds.items():
        period = feed.get("period", DEFAULT_POLL_INTERVAL)
        await ensure_task(bot, store, url, period)


@command("rss", role=Role.MODERATOR)
async def rss_command(bot, sender_jid, nick, args, msg, is_room):
    """
    Manage RSS feeds. Add/delete/list Feed URLs to your room. The feeds are
    checked every 20 minutes globally.

    Usage:
        {prefix}rss add <url>
        {prefix}rss delete <url>
        {prefix}rss list
    """
    store = bot.db.users.plugin("rss")
    if not args:
        bot.reply(msg, "Usage: rss <add|delete|list> ...")
        return
    sub = args[0].lower()

    room = None
    if is_room or (
        msg.get("type") in ("chat", "normal")
        and hasattr(msg["from"], "bare")
        and "@" in str(msg["from"].bare)
    ):
        room = msg["from"].bare

    if sub == "add":
        if len(args) != 2:
            bot.reply(
                msg,
                f"Usage: {config['prefix', ',']}rss add <url> (in a room or MUC DM only)",
            )
            return
        if not room:
            bot.reply(
                msg,
                "🔴  RSS add can only be used in a room or MUC DM.",
            )
            return
        url = _normalize_url(args[1])
        feeds = await get_feeds(store)
        if url not in feeds:
            try:
                feed = await fetch_feed(url)
                title = feed.feed.get("title", url)
                feed_link = feed.feed.get("link", url)
            except Exception as e:
                log.exception(f"Failed to fetch or parse feed {url}")
                bot.reply(msg, f"Failed to fetch or parse feed: {e}")
                return
            feeds[url] = {
                "title": title,
                "link": feed_link,
                "period": DEFAULT_POLL_INTERVAL,
                "rooms": [room],
                "last_id": None,
                "error_count": 0,
                "next_retry": 0,
            }
            await save_feeds(store, feeds)
            await ensure_task(bot, store, url, feeds[url]["period"])
            bot.reply(
                msg,
                f"✅ Added feed: {title} ({url}) every {DEFAULT_POLL_INTERVAL}s to {room}",
            )
        else:
            if room not in feeds[url]["rooms"]:
                feeds[url]["rooms"].append(room)
                await save_feeds(store, feeds)
                log.info(f"[RSS] ADD: {store}\n\n{feeds}")
                await ensure_task(
                    bot, store, url, feeds[url]["period"]
                )
                bot.reply(
                    msg,
                    f"✅ Added room {room} to feed:" +
                    f" {feeds[url]['title']} ({url})",
                )
            else:
                bot.reply(
                    msg,
                    f"ℹ️ Feed already added for this room: {url}",
                )
        return

    elif sub == "delete":
        if len(args) != 2:
            bot.reply(msg, "Usage: rss delete <url>")
            return
        if not room:
            bot.reply(
                msg,
                "🔴  RSS delete can only be used in a room or MUC DM.",
            )
            return
        url = _normalize_url(args[1])
        feeds = await get_feeds(store)
        log.info(f"[RSS] DELETE: {store}\n\n{feeds}")
        if url not in feeds:
            bot.reply(msg, "Feed not found.")
            return
        if room in feeds[url]["rooms"]:
            feeds[url]["rooms"].remove(room)
            if not feeds[url]["rooms"]:
                # No rooms left, remove feed
                feeds.pop(url)
                if url in CHECK_TASKS:
                    CHECK_TASKS[url].cancel()
                    del CHECK_TASKS[url]
                bot.reply(
                    msg,
                    f"🗑️ Deleted feed: {url} (no rooms left, feed removed)",
                )
            else:
                await save_feeds(store, feeds)
                await ensure_task(
                    bot, store, url, feeds[url]["period"]
                )
                bot.reply(
                    msg,
                    f"🗑️ Removed this room from feed: {url}",
                )
        else:
            bot.reply(
                msg,
                "ℹ️ This room was not subscribed to the feed.",
            )
        await save_feeds(store, feeds)
        return

    elif sub == "list":
        feeds = await get_feeds(store)
        if not feeds:
            bot.reply(msg, "No feeds configured.")
            return
        lines = ["📋 Watched RSS feeds:"]
        for feed_url, data in feeds.items():
            error_count = data.get("error_count", 0)
            status = ""
            if error_count > 0:
                status = f"  ⚠️ Last {error_count} fetch(es) failed\n"
            lines.append(
                f"- {feed_url}\n  Title: {data.get('title', feed_url)}\n"
                f"  Period: {data.get('period', '?')}s\n"
                f"  Rooms: {', '.join(data.get('rooms', []))}\n"
                f"{status}"
            )
        bot.reply(msg, lines)
    else:
        bot.reply(msg, "Unknown subcommand. Use add, delete, or list.")


async def on_load(bot):
    if feedparser is None:
        log.error(
            "[RSS] feedparser module not installed. RSS plugin will not work."
        )
        return
    await restart_all_tasks(bot)


async def on_unload(bot):
    """
    Clean up all RSS tasks on unload

    Prevents task orphaning and memory leaks
    """
    log.info("[RSS] Cleaning up RSS feed tasks...")

    # Cancel all active tasks
    for url, task in list(CHECK_TASKS.items()):
        try:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                log.debug(f"[RSS] Task for {url} cancelled")
            CHECK_TASKS.pop(url, None)
        except Exception as e:
            log.exception(f"[RSS] Error cancelling task for {url}: {e}")

    log.info("[RSS] ✅ All RSS tasks cleaned up")
