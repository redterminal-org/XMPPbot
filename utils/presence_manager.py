import logging

# === set up logging ===
log = logging.getLogger(__name__)


# -------------------------------------------------
# PresenceManager Class
# -------------------------------------------------

class PresenceManager:
    """
    Manages the presence status of the bot, including broadcasting
    presence updates and handling joined rooms.

    Attributes:
        bot: The bot instance using this manager.
        status: A dictionary containing the current presence 'show'
            and 'status' message.
        joined_rooms: A dictionary to track rooms the bot has joined.
        emojis: A mapping of presence states to emoji representations.
    """

    def __init__(self, bot):
        """
        Initialize the PresenceManager with a bot instance.

        Args:
            bot: The bot object that this manager will control presence for.
        """
        self.bot = bot

        self.status = {
            "show": "online",
            "status": "I'm ready to serve you!"
        }

        self.joined_rooms = {}

        self.emojis = {
            "online": "✅",
            "chat": "💬",
            "away": "👋 ",
            "xa": "💤",
            "dnd": "⛔"
        }

    def update(self, show, status):
        """
        Update the bot's presence status and broadcast the change.

        Args:
            show: The presence state (e.g., 'online', 'away').
            status: The status message to display.
        """
        self.status["show"] = show
        self.status["status"] = status

        self.broadcast()

    def broadcast(self):
        """
        Broadcast the current presence status to all relevant targets,
        including joined rooms if available. Logs the status update.
        """
        show = self.status.get("show", "online")
        status = self.status.get("status", "")

        try:
            self.bot.send_presence(pshow=show, pstatus=status)
        except Exception as e:
            log.exception(f"[PRESENCE] Failed to send presence: {e}")

        # --- Get JOINED_ROOMS from "rooms" plugin (safe access) ---
        try:
            rooms_plugin = self.bot.bot_plugins.plugins.get("rooms", None)
            if rooms_plugin is not None:
                # Make a defensive copy to avoid race conditions
                rooms_copy = dict(rooms_plugin.JOINED_ROOMS)
                for room, room_data in rooms_copy.items():
                    try:
                        nick = room_data.get("nick")
                        if nick:
                            self.bot.send_presence(
                                pto=f"{room}/{nick}",
                                pshow=show,
                                pstatus=status)
                    except Exception as e:
                        log.debug(
                            "[PRESENCE] Failed to send presence to room "
                            f"{room}: {e}")
        except Exception as e:
            log.debug(f"[PRESENCE] Error accessing rooms plugin: {e}")

        # log message
        log.info(f"[PRESENCE] {self.emoji(show)} Status set: "
                 f"'{show}': [{status}]")

    def emoji(self, show=None):
        """
        Get the emoji representation for a given presence state.

        Args:
            show: The presence state to get the emoji for. If None,
                uses the current status.

        Returns:
            str: The emoji corresponding to the presence state.
        """
        show = show or self.status.get("show", "online")
        return self.emojis.get(show, "")
