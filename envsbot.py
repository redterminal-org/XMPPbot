import slixmpp
import asyncio
import inspect
import logging
import os
import shutil
import subprocess

from slixmpp.xmlstream import ET

from datetime import datetime
from utils.presence_manager import PresenceManager
from utils.plugin_manager import PluginManager
from utils.rate_limiter import TokenBucketRateLimiter
from utils.config import (
    ConfigError,
    config,
    exit_on_config_error,
    setup_logging,
    validate_startup_config,
)
from utils.command import (
    resolve_command,
    check_permission,
    Role,
    role_from_int
)
from database.manager import DatabaseManager

# === set up logging ===
setup_logging()
log = logging.getLogger(__name__)


# -------------------------------------------------
# BOT CLASS
# -------------------------------------------------

class Bot(slixmpp.ClientXMPP):

    def __init__(self):
        # run __init__() from ClientXMPP
        super().__init__(config["jid"],
                         config["password"])

        self.nick = config.get("nick", "bot")
        self.admins = []
        self.prefix = config.get("prefix", ",")
        self.version = get_latest_git_tag() or "unknown"
        self.connection_start_time = None

        # Rate limiter (in-memory, per process)
        # capacity=4, refill 1 token every 0.5s
        self.rate_limiter = TokenBucketRateLimiter(
            capacity=4,
            refill_amount=1,
            refill_interval=0.5,
            deny_window=10.0,
            deny_threshold=6,
            base_block_seconds=30.0,
            backoff_multiplier=2.0,
            max_block_seconds=3600.0,
            notify_cooldown=10.0,
        )

        # Presence Manager
        self.presence = PresenceManager(self)

        self.register_plugin("xep_0012")
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0045")
        self.register_plugin("xep_0054")
        self.register_plugin("xep_0084")
        self.register_plugin("xep_0092")
        self.register_plugin("xep_0153")
        self.register_plugin("xep_0163")
        self.register_plugin("xep_0199")
        self.register_plugin("xep_0359")
        self.register_plugin("xep_0461")
        self.register_plugin("xep_0511")

        # Database Manager
        self.db = DatabaseManager(config.get("db", "bot.db"))

        # Plugin Manager
        self.bot_plugins = PluginManager(self)

        self.add_event_handler("session_start", self.on_start)
        self.add_event_handler("groupchat_message", self.on_muc_message)
        self.add_event_handler("message", self.on_private_message)

    # -------------------------------------------------
    # EVENT HANDLERS
    # -------------------------------------------------

    async def _send_restart_notification(self):
        """Send restart completion notification if one was queued."""
        import json
        import os

        restart_file = "/tmp/bot_restart_notification.json"

        if not os.path.exists(restart_file):
            return

        try:
            with open(restart_file, 'r') as f:
                notif = json.load(f)
            os.remove(restart_file)

            log.info("[ADMIN] Processing restart notification: %s", notif)

            if notif.get("is_room") and notif.get("room"):
                # Send to the room
                message = self.make_message(
                    mto=notif["room"],
                    mbody=f"{notif['nick']}: ✅ Bot restart complete!",
                    mtype="groupchat"
                )
                await self._safe_send_message(message)
                log.info("[ADMIN] Bot restart notification sent to room %s", notif["room"])
            else:
                # Send private message
                message = self.make_message(
                    mto=notif["sender"],
                    mbody="✅ Bot restart complete!",
                    mtype="chat"
                )
                await self._safe_send_message(message)
                log.info("[ADMIN] Bot restart notification sent to %s", notif["sender"])
        except FileNotFoundError:
            pass  # Expected if no restart notification
        except Exception as e:
            log.error("[ADMIN] Failed to process restart notification: %s", e)

    async def _safe_send_message(self, message):
        """
        Safely send a message with proper error handling.

        Handles both sync and async send() methods.
        Logs any errors that occur.

        Args:
            message: slixmpp Message object to send
        """
        try:
            result = message.send()
            # Check if send() is a coroutine (async)
            if inspect.iscoroutine(result):
                await result
            # log.info("[BOT] Message sent to %s: %s", message['to'], message['body'])
        except Exception as e:
            log.exception("[BOT] Failed to send message: %s", e)

    # fired on "session_start"
    async def on_start(self, event):
        self.connection_start_time = datetime.now()
        # send startup presence
        self.presence.broadcast()
        # Get roster
        await self.get_roster()
        # Connect to DB
        await self.db.connect()
        # load plugins
        await self.bot_plugins.load_all()

        # === CALL on_ready() HOOKS (after DB is ready) ===
        await self.bot_plugins.call_on_ready()

        # Check for restart notification (after everything is initialized)
        await self._send_restart_notification()

        # send presence again
        self.presence.broadcast()
        # set automatic mutual subscriptions
        self.roster.auto_subscribe = True

        log.info("[BOT] ✅ Bot started, all rooms joined")

    # fired when a MUC room message arrives
    async def on_muc_message(self, msg):
        try:
            room = msg['from'].bare
            nick = msg.get('mucnick')  # Use .get() for safety

            # ignore messages from ourselves (our nick)
            bot_nick = self.presence.joined_rooms.get(room)
            if bot_nick == nick:
                return

            # proceed to command handling
            if msg["type"] == "groupchat":
                await self.handle_command(
                    msg["body"],
                    msg["from"],
                    nick,
                    msg,
                    True
                )
        except Exception as e:
            log.exception("[BOT] Error in on_muc_message: %s", e)

    # fired when a direct message to the bot or a MUC DM arrives
    async def on_private_message(self, msg):
        try:
            if msg["type"] in ("chat", "normal"):
                await self.handle_command(
                    msg["body"],
                    msg["from"],
                    None,
                    msg,
                    False
                )
        except Exception as e:
            log.exception("[BOT] Error in on_private_message: %s", e)

    # -------------------------------------------------
    # HELPER FUNCTIONS
    # -------------------------------------------------

    async def get_user_role(self, jid, room=None) -> Role:
        """
        Resolve a user's role using config and database.
        """
        import slixmpp
        from plugins.rooms import JOINED_ROOMS

        try:
            jid = slixmpp.JID(jid).bare
        except Exception as e:
            log.warning("[BOT] Failed to parse JID '%s': %s", jid, e)
            return Role.NONE

        try:
            owner_jid = slixmpp.JID(config["owner"]).bare
        except Exception as e:
            log.warning("[BOT] Failed to parse owner JID: %s", e)
            owner_jid = None

        # owner override
        if owner_jid and jid == owner_jid:
            return Role.OWNER

        row = await self.db.users.get(jid)

        if row is None:
            return Role.NONE

        try:
            db_role = role_from_int(row['role'])
        except KeyError:
            return Role.NONE

        # Elevate to MODERATOR if user is admin/owner in any joined room
        if room:
            room_info = JOINED_ROOMS.get(room)
            if room_info:
                nicks = room_info.get("nicks", {})
                for nick_info in nicks.values():
                    try:
                        if str(nick_info.get("jid")) == str(jid):
                            affiliation = nick_info.get("affiliation", "")
                            if (affiliation in ("admin", "owner")
                                    and db_role > Role.MODERATOR):
                                return Role.MODERATOR
                    except Exception as e:
                        log.debug("[BOT] Error checking room affiliation: %s", e)
        return db_role

    def reply(self, msg, text, mention=True, thread=True, rate_limit=True,
              ephemeral=False):
        """
        Smart reply helper for plugins.

        Features:
        - Mentions the sender in group chats
        - Supports message threading
        - Formats multi-line responses
        - Basic per-user rate limiting
        - Safe message sending

        Args:
            msg: Original message object
            text (str|list): Reply text or list of lines
            mention (bool): Mention sender in group chats
            thread (bool): Thread reply if possible
            rate_limit (bool): Apply anti-spam limit
            ephemeral (bool): Mark as ephemeral (no-store)
        """

        # Convert list responses into multi-line text
        if isinstance(text, list):
            text = "\n".join(text)

        msg_type = msg.get("type", "chat")

        if msg_type == "groupchat":

            body = text

            if mention:
                nick = msg.get("mucnick") or msg["from"].resource
                body = f"{nick}: {text}"

            try:
                message = self.make_message(
                    mto=msg["from"].bare,
                    mbody=body,
                    mtype="groupchat"
                )

                if thread:
                    thread_id = msg.get("thread") or msg.get("id")
                    if thread_id:
                        try:
                            message["thread"] = thread_id
                        except Exception:
                            log.debug("[BOT] Setting thread failed!")

                # Make reply ephemeral
                if ephemeral:
                    message.append(ET.Element("{urn:xmpp:hints}no-store"))

                # send reply safely
                asyncio.create_task(self._reply_send_wrapper(message))

                # log message
                # log.info(f"[BOT] Replying in room {msg['from'].bare} to {msg.get('mucnick')}: {text}")

                # support test MockMessage
                if hasattr(msg, "replies"):
                    msg.replies.append(text)

            except Exception as e:
                log.exception("[BOT] Error creating groupchat reply: %s", e)

        else:

            try:
                message = self.make_message(
                    mto=msg["from"],
                    mbody=text,
                    mtype="chat"
                )

                if thread:
                    thread_id = msg.get("thread") or msg.get("id")
                    if thread_id:
                        try:
                            message["thread"] = thread_id
                        except Exception:
                            pass

                # Make reply ephemeral
                message.append(ET.Element("{urn:xmpp:hints}no-store"))

                # log message
                # log.info(f"[BOT] Replying to {msg['from']}: {text}")

                # send reply safely
                asyncio.create_task(self._reply_send_wrapper(message))

                # support test MockMessage
                if hasattr(msg, "replies"):
                    msg.replies.append(text)

            except Exception as e:
                log.exception("[BOT] Error creating private reply: %s", e)

    async def _reply_send_wrapper(self, message):
        """
        Wrapper to send messages asynchronously with error handling.
        """
        try:
            await self._safe_send_message(message)
        except Exception as e:
            log.exception("[BOT] Error in reply send wrapper: %s", e)

    # -------------------------------------------------
    # UNIFIED COMMAND HANDLER
    # -------------------------------------------------

    async def handle_command(self, body, sender_jid, nick, msg, is_room):
        """
        Parse and execute a bot command from a message.

        This method is called by both private-message and groupchat
        handlers.  It checks whether the message begins with the
        configured command prefix, resolves the command using the
        command resolver, verifies user permissions, and executes
        the command handler.

        Workflow
        --------
        1. Validate that the message body exists and begins with the command
           prefix.
        2. Strip the prefix and process rate limiting
        3. Resolve the command using `resolve_command()`.
        4. Determine the sender's role (owner, admin, moderator, user, none).
        5. Verify that the user has permission to execute the command.
        6. Execute the command handler (async or sync).
        7. Catch and report execution errors.

        Parameters
        ----------
        body : str
            Raw message body received from the XMPP message.
        sender_jid : str
            JID of the message sender.
        nick : str
            Nickname of the sender in a groupchat. May be None for private
            messages.
        msg : slixmpp.Message
            Original Slixmpp message object used for replies and metadata.
        is_room : bool
            True if the message was received in a MUC (groupchat), False if it
            was a private chat message.

        Notes
        -----
        - Commands are detected using the bot's configured prefix (e.g. ",").
        - Command resolution supports:
            * multi-word commands
            * command aliases
            * longest-match detection
        - Permission checks are based on the role hierarchy:
                    OWNER = 1
                    SUPERADMIN = 10
                    ADMIN = 20
                    MODERATOR = 40
                    TRUSTED = 60
                    USER = 80
                    NEW = 90
                    NONE = 95
                    BANNED = 100

          Lower numbers have higher privileges.

        - Errors are logged and a user-friendly message is returned to the
          sender.
        """

        if not body:
            return
        if not body.startswith(self.prefix):
            return
        text = body[len(self.prefix):].strip()
        if not text:
            return

        # Checking for real JID
        jid = None
        room = None
        muc = self.plugin.get("xep_0045", None)

        try:
            if muc:
                room = msg['from'].bare
                nick = msg.get("mucnick") or msg["from"].resource
                jid = muc.get_jid_property(room, nick, "jid")
        except Exception as e:
            log.debug("[BOT] Error getting JID from MUC: %s", e)

        # Fallback to sender_jid if JID resolution failed
        if jid is None:
            jid = sender_jid
        else:
            try:
                jid = str(slixmpp.JID(jid).bare)
            except Exception as e:
                log.warning("[BOT] Failed to parse resolved JID: %s", e)
                jid = str(sender_jid)

        # Apply rate limiting on ingress
        allowed, retry_after = await self.rate_limiter.allow(jid)
        if not allowed:
            # Avoid notifying the whole room; log and occasional
            # admin notification only
            if self.rate_limiter.notify_allowed(jid):
                log.info(("[BOT] 🟡️ Rate-limited %s "
                          "in room %s (retry_after=%.1fs)"),
                         jid, room, retry_after)
            return

        # --- resolve command using command resolver ---
        cmd_obj, args = resolve_command(text)

        if not cmd_obj:
            return

        cmd_name = cmd_obj.name

        # determine sender role
        user_role = await self.get_user_role(jid, room)

        # permission check
        if not check_permission(user_role, cmd_obj):
            self.reply(msg, "🔴 You are not allowed to use this command.")
            return

        # Commands which require permissions of at least "moderator"
        # shouldn't be used in GroupChat
        required_role = getattr(cmd_obj, "role", Role.NONE)
        if required_role <= Role.MODERATOR and is_room:
            self.reply(msg, "🔴 Use this command in MUC Direct Message only.")
            return

        try:
            handler = getattr(cmd_obj, "handler", None)
            if not handler:
                log.error(f"[BOT]🔴 Command '{cmd_name}' has no handler")
                return
            result = handler(self, sender_jid, nick, args, msg, is_room)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            log.exception(f"[BOT]🔴  Error while executing command '{cmd_name}'")
            if user_role in (Role.OWNER, Role.ADMIN):
                err_msg = f"🔴 Command error: {e}"
            else:
                err_msg = (f"🔴 Command '{cmd_name}' "
                           f"failed due to internal error.")

            self.reply(msg, err_msg)


# --------------------------------------------------
# GET LATEST GIT TAG (for version display)
# --------------------------------------------------
def get_latest_git_tag():
    try:
        tag = subprocess.check_output(
            ['git', 'describe', '--tags', '--abbrev=0'],
            stderr=subprocess.STDOUT
        ).strip().decode('utf-8')
        return tag
    except subprocess.CalledProcessError:
        return None  # No tags found or not a git repo


# -------------------------------------------------
# MAIN FUNCTION
# -------------------------------------------------

async def main():
    try:
        validate_startup_config(config)
    except ConfigError as e:
        exit_on_config_error(e)

    xmpp = Bot()

    # startup bot
    host = config.get("host", None)
    port = config.get("port", None)

    if host or port:
        await xmpp.connect(host=host or xmpp.boundjid.domain,
                           port=port or 5222)
    else:
        await xmpp.connect()

    log.info("[XMPP] ✅ Connected successfully. Starting event loop...")

    try:
        await xmpp.disconnected
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Gracefully shut down on CTRL-c
        log.info("[XMPP] Shutdown request")

        xmpp.disconnect()

        try:
            await asyncio.wait_for(xmpp.disconnected, timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("[XMPP] Disconnect timeout")

    finally:
        log.info("[XMPP] disconnected. Closing Database...")
        try:
            await xmpp.db.close()
        except Exception as e:
            log.exception(f"[XMPP] Error closing database: {e}")
        log.info("[XMPP] ✅ Database closed! End!")

if __name__ == "__main__":
    SOURCE = "init_chat_slang.csv"
    TARGET = "chat_slang.csv"
    if os.path.exists(SOURCE) and not os.path.exists(TARGET):
        try:
            shutil.copyfile(SOURCE, TARGET)
            log.info(f"[INIT] ✅ Copied {SOURCE} to {TARGET}")
        except Exception as e:
            log.error(f"[INIT] 🔴 Failed to copy {SOURCE} to {TARGET}: {e}")
    elif not os.path.exists(SOURCE):
        log.warning(f"[INIT] 🔴 Source file {SOURCE} not found. Skipping copy.")
    else:
        log.info(f"[INIT] ✅ Target file {TARGET} already exists. Skipping copy.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
