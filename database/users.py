import json
import logging
import asyncio
from datetime import datetime, timezone

GLOBAL_JID = "__GLOBAL__"

log = logging.getLogger(__name__)


class PluginRuntimeStore:
    """
    Cache-backed runtime storage for plugin-specific user data.

    This store provides a per-plugin interface to the shared `users_runtime`
    table, which stores a single JSON blob per user (jid). The structure of
    that JSON is expected to be:

        {
            "plugins": {
                "<plugin_name>": { ... plugin-specific data ... }
            }
        }

    Key characteristics:
    - Read-through cache: data is loaded from the database on first access.
    - Write-behind cache: all mutations are applied in-memory and marked dirty.
    - No immediate database writes: persistence happens later via
      UserManager.flush_*.
    - Per-plugin isolation: each plugin only accesses its own namespace inside
      the shared JSON document.

    Important invariants:
    - `_runtime_cache[jid]` always contains the full JSON blob for that user.
    - `_dirty_runtime` tracks jids whose runtime data must be flushed.
    - The UserManager is responsible for writing cached data to the database,
      typically in the order: users → runtime.

    This design ensures:
    - High performance (fewer DB writes)
    - Consistent state across related tables
    - Compatibility with existing JSON-based SQL queries
    """

    def __init__(self, user_manager, plugin_name: str):
        self.um = user_manager
        self.plugin_name = plugin_name

    # ------------------------------------------------------------------
    # INTERNAL
    # ------------------------------------------------------------------

    async def _load_from_db(self, jid: str) -> dict:
        """
        Load full runtime JSON for a user from the database.
        Ensures the returned structure always contains a "plugins" dict.
        """
        cursor = await self.um.db.execute(
            "SELECT data, last_updated FROM users_runtime WHERE jid = ?",
            (jid,),
        )
        row = await cursor.fetchone()

        if not row:
            self.um._runtime_meta[jid] = None
            return {"plugins": {}}
        if row[0] is None:
            self.um._runtime_meta[jid] = None
            return {"plugins": {}}

        raw_data, last_updated = row

        try:
            data = json.loads(raw_data)
        except Exception:
            log.exception("[RUNTIME] Failed to decode JSON for %s", jid)
            return {"plugins": {}}

        if "plugins" not in data:
            data["plugins"] = {}

        # Store timestamp in meta
        self.um._runtime_meta[jid] = last_updated

        return data

    def _ensure_cache(self, jid: str):
        """
        Ensure runtime cache structure exists for the given jid and plugin.
        """
        if jid not in self.um._runtime_cache:
            self.um._runtime_cache[jid] = {"plugins": {}}

        if "plugins" not in self.um._runtime_cache[jid]:
            self.um._runtime_cache[jid]["plugins"] = {}

        if self.plugin_name not in self.um._runtime_cache[jid]["plugins"]:
            self.um._runtime_cache[jid]["plugins"][self.plugin_name] = {}

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------
    async def get_global(self, key, default=None):
        """
        Get plugin-global value (not tied to a user).
        """
        data = await self.get(GLOBAL_JID, key)
        return default if data is None else data

    async def set_global(self, key, value):
        """
        Set plugin-global value.
        """
        await self.set(GLOBAL_JID, key, value)

    async def get(self, jid: str, key: str = None):
        """
        Retrieve runtime data for this plugin.

        If the user is not yet cached, data is loaded from the database.

        Args:
            jid: User JID
            key: Optional key within the plugin's data

        Returns:
            - Full plugin data dict if key is None
            - Value for the given key otherwise (or None if missing)
        """
        if jid not in self.um._runtime_cache:
            self.um._runtime_cache[jid] = await self._load_from_db(jid)

        data = self.um._runtime_cache[jid]

        if "plugins" not in data:
            data["plugins"] = {}

        plugin_data = data["plugins"].setdefault(self.plugin_name, {})

        if key is None:
            return plugin_data

        return plugin_data.get(key)

    async def set(self, jid: str, key: str, value):
        """
        Set a runtime value for this plugin (cached only).

        Marks the user as dirty so the change will be persisted on flush.
        """

        # Get update time
        now = datetime.now(timezone.utc).isoformat()

        if jid not in self.um._runtime_cache:
            self.um._runtime_cache[jid] = await self._load_from_db(jid)

        data = self.um._runtime_cache[jid]

        if "plugins" not in data:
            data["plugins"] = {}

        plugin_data = data["plugins"].setdefault(self.plugin_name, {})

        plugin_data[key] = value

        self.um._runtime_meta[jid] = now
        self.um._dirty_runtime.add(jid)

    async def delete(self, jid: str, key: str):
        """
        Delete a key from this plugin's runtime data (cached).
        """
        now = datetime.now(timezone.utc).isoformat()

        if jid not in self.um._runtime_cache:
            self.um._runtime_cache[jid] = await self._load_from_db(jid)

        data = self.um._runtime_cache[jid]

        plugin_data = data.get("plugins", {}).get(self.plugin_name, {})

        if key in plugin_data:
            del plugin_data[key]
            self.um._runtime_meta[jid] = now
            self.um._dirty_runtime.add(jid)

    async def clear(self, jid: str):
        """
        Remove all runtime data for this plugin (cached).
        """
        now = datetime.now(timezone.utc).isoformat()

        if jid not in self.um._runtime_cache:
            self.um._runtime_cache[jid] = await self._load_from_db(jid)

        data = self.um._runtime_cache[jid]

        if "plugins" not in data:
            data["plugins"] = {}

        data["plugins"][self.plugin_name] = {}

        self.um._runtime_meta[jid] = now
        self.um._dirty_runtime.add(jid)


class UserManager:
    """
    Manages users + in-memory cache.

    Responsibilities:
    - Cache users, runtime
    - Provide helper functions (get_value, set_value)
    - Manage users table

    Does NOT:
    - Write JSON blobs (handled by stores)
    - Parse JSON (handled by SQLite JSON1)
    """

    def __init__(self, db):
        self.db = db
        self._nick_index = {}
        self._nick_index_lock = asyncio.Lock()

        self._users_cache = {}
        self._runtime_cache = {}

        self._runtime_meta = {}

        self._dirty_users = set()
        self._dirty_runtime = set()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def ensure_global_exists(self):
        if await self.get(GLOBAL_JID) is None:
            await self.create(GLOBAL_JID, "__global__")

    async def init(self):
        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            jid TEXT PRIMARY KEY,
            nickname TEXT,
            role INTEGER DEFAULT 80,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            registered INTEGER DEFAULT FALSE
        )
        """)

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS users_runtime (
            jid TEXT PRIMARY KEY,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            data TEXT DEFAULT '{}' NOT NULL,
            FOREIGN KEY (jid)
                REFERENCES users(jid)
                ON DELETE CASCADE
                ON UPDATE CASCADE
        )
        """)

        # Ensure GLOBAL_JID exists
        await self.ensure_global_exists()

        # Load persisted _nick_index
        store = self.plugin("users")
        index = await store.get_global("_nick_index")

        if isinstance(index, dict):
            # Convert all lists to sets for consistency
            self._nick_index = {
                nick: set(jids) if isinstance(jids, list) else jids
                for nick, jids in index.items()
            }

    # ------------------------------------------------------------------
    # Users (DB + cache)
    # ------------------------------------------------------------------

    async def create(self, jid, nickname=None):
        now = datetime.now(timezone.utc).isoformat()
        if jid not in self._users_cache:
            self._users_cache[jid] = {
                "jid": jid,
                "nickname": nickname,
                "role": 80,
                "created_at": now,
                "last_seen": now,
                "registered": True,
            }
            self._dirty_users.add(jid)

    async def get(self, jid):
        if jid in self._users_cache:
            return self._users_cache[jid]

        cursor = await self.db.execute(
            "SELECT * FROM users WHERE jid=?",
            (jid,)
        )
        row = await cursor.fetchone()

        if not row:
            return None

        user = dict(row)
        self._users_cache[jid] = user
        return user

    async def set(self, jid, key, value):
        user = await self.get(jid)
        if not user:
            return None
        user[key] = value
        self._dirty_users.add(jid)
        return user

    async def update_last_seen(self, jid):
        now = datetime.now(timezone.utc).isoformat()
        await self.set(jid, "last_seen", now)

    async def delete(self, jid):
        # 1. Delete from database
        await self.db.execute(
            "DELETE FROM users WHERE jid = ?",
            (jid,)
        )

        await self.db.execute(
            "DELETE FROM users_runtime WHERE jid = ?",
            (jid,)
        )

        # 2. Remove from caches
        self._users_cache.pop(jid, None)
        self._runtime_cache.pop(jid, None)

        # 3. Clean dirty flags
        self._dirty_users.discard(jid)
        self._dirty_runtime.discard(jid)

        # 4. Remove from _nick_index
        for nick in list(self._nick_index.keys()):
            jids = self._nick_index[nick]
            # Convert to set if needed (for robustness)
            if not isinstance(jids, set):
                jids = set(jids) if isinstance(jids, list) else {jids}

            if jid in jids:
                jids.discard(jid)
                if not jids:
                    del self._nick_index[nick]
                else:
                    self._nick_index[nick] = jids

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def get_value(self, data, key_path):
        keys = key_path.split(".")
        value = data

        for k in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(k)
            if value is None:
                return None

        return value

    async def set_value(self, cache, dirty, jid, key_path, value):
        data = cache.setdefault(jid, {})

        keys = key_path.split(".")
        target = data

        for k in keys[:-1]:
            target = target.setdefault(k, {})

        target[keys[-1]] = value
        dirty.add(jid)

    # ------------------------------------------------------------------
    # FLUSH LOGIC
    # ------------------------------------------------------------------

    async def flush_users(self):
        for jid in self._dirty_users:
            user = self._users_cache[jid]

            await self.db.execute(
                """
                INSERT INTO users (jid, nickname, role, last_seen, registered)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(jid)
                DO UPDATE SET
                    nickname=excluded.nickname,
                    role=excluded.role,
                    last_seen=excluded.last_seen,
                    registered=excluded.registered
                """,
                (
                    user["jid"],
                    user.get("nickname"),
                    user.get("role", 80),
                    user.get("last_seen"),
                    user.get("registered", 0),
                )
            )

    async def _write_runtime(self, jid: str, data: dict):
        """
        Persist full runtime JSON blob for a user.

        Uses UPSERT semantics to either insert or update the row.
        """
        timestamp = self._runtime_meta.get(jid)
        await self.db.execute(
            """
            INSERT INTO users_runtime (jid, last_updated, data)
            VALUES (?, ?, ?)
            ON CONFLICT(jid)
            DO UPDATE SET
                last_updated = excluded.last_updated,
                data = excluded.data
            """,
            (jid, timestamp, json.dumps(data)),
        )

    async def flush_all(self):
        """
        Flush all cached data atomically in a single transaction.
        """
        # Persist nick index
        index = getattr(self, "_nick_index", None)
        if index is not None:
            store = self.plugin("users")
            # Convert sets to lists for JSON serialization
            serializable_index = {
                nick: list(jids) if isinstance(jids, set) else jids
                for nick, jids in index.items()
            }
            await store.set_global("_nick_index", serializable_index)

        if not (self._dirty_users or self._dirty_runtime):
            return

        # Start transaction using SAVEPOINT (thread-safe alternative)
        try:
            await self.db.execute("SAVEPOINT flush_checkpoint")

            # ----------------------------------------------------------
            # 1. USERS
            # ----------------------------------------------------------
            if self._dirty_users:
                await self.flush_users()

            # ----------------------------------------------------------
            # 2. RUNTIME
            # ----------------------------------------------------------
            for jid in self._dirty_runtime:
                data = self._runtime_cache.get(jid) or {"plugins": {}}
                await self._write_runtime(jid, data)

            # Commit transaction
            await self.db.execute("RELEASE flush_checkpoint")
            log.debug("[DB] ✅ UserManager.flush_all() SUCCESSFUL!")

        except Exception:
            try:
                await self.db.execute("ROLLBACK TO flush_checkpoint")
            except Exception:
                pass  # Rollback might also fail
            log.exception("[DB] FLUSH ALL FAILED!")
            raise

        # ----------------------------------------------------------
        # CLEAR DIRTY FLAGS AFTER SUCCESS
        # ----------------------------------------------------------
        self._dirty_users.clear()
        self._dirty_runtime.clear()

    # ------------------------------------------------------------------
    # Plugin API
    # ------------------------------------------------------------------

    def plugin(self, plugin_name: str):
        return PluginRuntimeStore(self, plugin_name)
