import json

DEFAULT_STATUS = json.dumps({})


class Rooms:
    """
    Rooms table manager.
    """

    def __init__(self, conn):

        self.conn = conn

    async def init(self):

        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                room_jid TEXT PRIMARY KEY,
                nick TEXT,
                autojoin INTEGER DEFAULT 0,
                status TEXT DEFAULT '{}'
            )
            """
        )

        await self.conn.commit()

    async def add(self, room_jid, nick, autojoin=False):

        status = json.dumps({})

        await self.conn.execute(
            "INSERT OR REPLACE INTO rooms (room_jid, nick, autojoin, status)"
            " VALUES (?, ?, ?, ?)",
            (room_jid, nick, int(autojoin), status)
        )

        await self.conn.commit()

    async def delete(self, room_jid):

        await self.conn.execute(
            "DELETE FROM rooms WHERE room_jid = ?",
            (room_jid,)
        )

        await self.conn.commit()

    async def update(self, room_jid, **fields):
        # Only allow updates to these columns
        allowed_fields = {"nick", "autojoin", "status"}
        safe_fields = {k: v for k, v in fields.items() if k in allowed_fields}
        if not safe_fields:
            return

        keys = ", ".join(f"{k}=?" for k in safe_fields)
        values = list(safe_fields.values())

        await self.conn.execute(
            f"UPDATE rooms SET {keys} WHERE room_jid=?",
            (*values, room_jid)
        )

        await self.conn.commit()

    async def list(self):

        cursor = await self.conn.execute(
            "SELECT room_jid, nick, autojoin, status FROM rooms"
        )

        return await cursor.fetchall()

    async def get(self, room_jid):

        cursor = await self.conn.execute(
            "SELECT room_jid, nick, autojoin, status FROM rooms WHERE"
            " room_jid=?",
            (room_jid,)
        )

        return await cursor.fetchone()

    # ----------------
    # Helper functions
    # ----------------
    def _get_nested(self, data, path):

        keys = path.split(".")
        current = data

        for k in keys:
            if not isinstance(current, dict) or k not in current:
                return None
            current = current[k]

        return current

    def _set_nested(self, data, path, value):

        keys = path.split(".")
        current = data

        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]

        current[keys[-1]] = value

    async def status_get(self, room_jid, path=None):

        row = await self.get(room_jid)

        if not row:
            return None

        status = row[3] or "{}"
        data = json.loads(status)

        if path is None:
            return data

        return self._get_nested(data, path)

    async def status_set(self, room_jid, path, value):

        row = await self.get(room_jid)

        if not row:
            return

        status = row[3] or "{}"
        data = json.loads(status)

        self._set_nested(data, path, value)

        await self.conn.execute(
            "UPDATE rooms SET status=? WHERE room_jid=?",
            (json.dumps(data), room_jid)
        )

        await self.conn.commit()

    async def status_delete(self, room_jid, path):

        row = await self.get(room_jid)

        if not row:
            return

        status = row[3] or "{}"
        data = json.loads(status)

        keys = path.split(".")
        current = data

        for k in keys[:-1]:
            if k not in current:
                return
            current = current[k]

        current.pop(keys[-1], None)

        await self.conn.execute(
            "UPDATE rooms SET status=? WHERE room_jid=?",
            (json.dumps(data), room_jid)
        )

        await self.conn.commit()
