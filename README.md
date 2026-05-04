# envsbot

---

A modular XMPP bot built with Python 3 and slixmpp.

---

**Mirrors:**
- https://git.envs.net/dan/envsbot
- https://github.com/dan-envs/envsbot

---

## 🌐 envs pubnix/tilde

envsbot is developed with the **envs pubnix** environment in mind, but is not limited to it. It takes the tildebot IRC bot as model and hopefully will include all of its features and more (especially in XMPP groupchats and DMs).

---

## About

envsbot is now in a usable state: the core framework is mostly stable, although probably not bug-free, supports dynamic plugin loading, and provides a structured command system. We are now developing new plugins and features on top of it.

- Plugin-based architecture
- Dynamic plugin loading/reloading
- Command decorators
- SQLite-backed database layer

---

## Available Plugins

Below is a complete list of Python plugins currently available in `plugins/`, each with a short summary.

### **_admin**
> Administrative bot management commands for restart, shutdown, and runtime status/statistics.

### **_core**
> Internal shared helper plugin providing common utilities for other plugins, such as JID resolution, room permission checks, and room toggle helpers.

### **_reg_profile**
> Bot profile initialization plugin. Publishes or updates the bot's vCard and avatar on startup or reload, avoiding unnecessary network updates when nothing changed.

### **birthday_notify**
> Automatic birthday notification plugin for rooms. Announces birthdays for present users in opted-in rooms, with per-room enable/disable support and cached vCard birthday lookups.

### **dice**
> Dice rolling plugin with support for standard dice notation, modifiers, and optional success/failure target checks.

### **ducks**
> Duck game plugin for MUCs. Randomly spawns ducks in enabled rooms, lets users befriend or trap them, and keeps persistent stats and leaderboards.

### **help**
> Dynamic help system for plugins and commands, including multi-word commands and per-room in-room help toggling.

### **information**
> Information lookup plugin with commands for Wikipedia summaries, latest Fediverse posts, and Urban Dictionary searches, with per-room toggling.

### **karma**
> Room-local karma tracking plugin using `nick++` / `nick--`, with leaderboards and per-room enable/disable support.

### **pin**
> Room pinning plugin for saving, listing, showing, and deleting pinned messages, including reply-based pinning and fallback pinning of recent messages.

### **plugins**
> Runtime plugin management commands for listing, loading, unloading, reloading, and inspecting plugins.

### **poll**
> Room poll plugin with multiple simultaneous polls, voting, history, optional timed auto-close, and moderation/creator management controls.

### **reminder**
> Reminder scheduling plugin that lets users create and receive timed reminders after specified intervals.

### **rooms**
> Room management and persistence plugin for managing joined MUC rooms, autojoin behavior, and related room configuration.

### **rss**
> RSS/Atom feed watcher plugin that monitors subscribed feeds and posts new entries into configured rooms.

### **sed**
> Sed-style message correction plugin for fixing previous messages with regex or literal substitutions, with per-room enable/disable support.

### **status**
> Bot presence/status plugin for viewing and changing the bot's XMPP presence state and optional status message.

### **tell**
> Offline message plugin that stores messages for users and delivers them when they join the room again.

### **tools**
> General utility plugin with commands like ping/pong, echo, time/date lookups by timezone, UTC display, and Unix timestamp conversion.

### **urlcheck**
> URL metadata plugin that watches room messages for links and posts page titles, descriptions, file info, or YouTube metadata while avoiding duplicate spam.

### **users**
> User management plugin with automatic user registration, last-seen tracking, room nickname tracking, user lookup, role changes, and user deletion.

### **vcard**
> vCard lookup and profile plugin for retrieving public user profile information such as names, birthdays, URLs, organization, and location-related fields.

### **weather**
> Weather plugin that shows current weather for a user's configured vCard location, usable in rooms, MUC DMs, or direct messages.

### **xkcd**
> XKCD plugin that fetches latest, specific, random, or searched comics and can automatically post new comics to subscribed rooms.

### **xmpp**
> XMPP utility plugin with diagnostics and lookup commands such as ping, version, service discovery, uptime, SRV lookups, and compliance checks.

---

## Installation

1. **Clone the repository:**
   ```sh
   git clone https://github.com/yourusername/envsbot.git
   cd envsbot
   ```

2. **Create a virtual environment (recommended):**
   ```sh
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```

4. **Configure the bot:**
    - Copy `config_sample.json` to `config.json` and edit with your XMPP credentials and settings.

5. **Run the bot:**
   ```sh
   python envsbot.py
   ```

---

## TODO

- [X] Plugin Management Plugin [core]
- [X] User Management Plugin [core]
- [X] Room Management Plugin [core]
- [ ] Add more plugins
- [ ] Improve documentation and usage examples
- [ ] Enhance error handling and logging
- [ ] Choosable Plugins on startup in configuration file
- [X] Improve documentation for configuration file

---

## License

This project is licensed under the **GPL-3.0-only** License. See the [LICENSE](LICENSE) file for details. Future versions of the GPL License are explicitly

