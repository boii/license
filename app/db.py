"""SQLite kecil. Sengaja dijaga simple, tidak pakai ORM."""
from __future__ import annotations

import os
import secrets
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    key            TEXT PRIMARY KEY,
    product        TEXT NOT NULL DEFAULT 'default',
    owner          TEXT,                    -- catatan bebas: email/nama/notes
    status         TEXT NOT NULL DEFAULT 'active',  -- active | revoked
    max_machines   INTEGER NOT NULL DEFAULT 1,
    expires_at     INTEGER,                 -- unix seconds, NULL = lifetime
    created_at     INTEGER NOT NULL,
    created_by     INTEGER                  -- telegram user id
);

CREATE TABLE IF NOT EXISTS activations (
    license_key  TEXT NOT NULL,
    machine_id   TEXT NOT NULL,
    fingerprint  TEXT,
    last_seen    INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (license_key, machine_id),
    FOREIGN KEY (license_key) REFERENCES licenses(key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_act_key ON activations(license_key);

-- Log penggunaan. Sengaja TANPA foreign key agar audit trail tetap ada
-- meski lisensinya dihapus.
CREATE TABLE IF NOT EXISTS usage_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    license_key  TEXT,
    machine_id   TEXT,
    product      TEXT,
    event        TEXT NOT NULL,    -- validate | activate | deactivate
    status       TEXT NOT NULL,    -- ok | not_found | revoked | expired | ...
    ip           TEXT,
    user_agent   TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_key_time ON usage_events(license_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_time ON usage_events(created_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # tanpa karakter ambigu


def generate_key(groups: int = 4, group_len: int = 5) -> str:
    parts = []
    for _ in range(groups):
        parts.append("".join(secrets.choice(_ALPHABET) for _ in range(group_len)))
    return "-".join(parts)


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as conn:
            await conn.executescript(SCHEMA)
            await conn.commit()

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    # --- license ops ---
    async def create_license(
        self,
        *,
        product: str = "default",
        owner: str | None = None,
        max_machines: int = 1,
        expires_at: int | None = None,
        created_by: int | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        key = key or generate_key()
        now = int(time.time())
        async with self._connect() as conn:
            await conn.execute(
                "INSERT INTO licenses(key, product, owner, status, max_machines, expires_at, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (key, product, owner, "active", max_machines, expires_at, now, created_by),
            )
            await conn.commit()
        return await self.get_license(key)  # type: ignore[return-value]

    async def get_license(self, key: str) -> dict[str, Any] | None:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM licenses WHERE key=?", (key,))
            row = await cur.fetchone()
            if not row:
                return None
            data = dict(row)
            cur = await conn.execute(
                "SELECT COUNT(*) FROM activations WHERE license_key=?", (key,)
            )
            (count,) = await cur.fetchone()  # type: ignore[misc]
            data["activations"] = count
            return data

    async def list_licenses(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT key, product, owner, status, max_machines, expires_at, created_at "
                "FROM licenses ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_status(self, key: str, status: str) -> bool:
        async with self._connect() as conn:
            cur = await conn.execute(
                "UPDATE licenses SET status=? WHERE key=?", (status, key)
            )
            await conn.commit()
            return cur.rowcount > 0

    async def set_expiry(self, key: str, expires_at: int | None) -> bool:
        async with self._connect() as conn:
            cur = await conn.execute(
                "UPDATE licenses SET expires_at=? WHERE key=?", (expires_at, key)
            )
            await conn.commit()
            return cur.rowcount > 0

    async def set_max_machines(self, key: str, n: int) -> bool:
        async with self._connect() as conn:
            cur = await conn.execute(
                "UPDATE licenses SET max_machines=? WHERE key=?", (n, key)
            )
            await conn.commit()
            return cur.rowcount > 0

    async def delete_license(self, key: str) -> bool:
        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys=ON")
            cur = await conn.execute("DELETE FROM licenses WHERE key=?", (key,))
            await conn.commit()
            return cur.rowcount > 0

    # --- activations ---
    async def list_activations(self, key: str) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT machine_id, fingerprint, last_seen, created_at "
                "FROM activations WHERE license_key=? ORDER BY created_at DESC",
                (key,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_activation(
        self, key: str, machine_id: str, fingerprint: str | None
    ) -> tuple[bool, int]:
        """Returns (is_new, total_after)."""
        now = int(time.time())
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM activations WHERE license_key=? AND machine_id=?",
                (key, machine_id),
            )
            existed = await cur.fetchone() is not None
            if existed:
                await conn.execute(
                    "UPDATE activations SET last_seen=?, fingerprint=COALESCE(?, fingerprint) "
                    "WHERE license_key=? AND machine_id=?",
                    (now, fingerprint, key, machine_id),
                )
            else:
                await conn.execute(
                    "INSERT INTO activations(license_key, machine_id, fingerprint, last_seen, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (key, machine_id, fingerprint, now, now),
                )
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM activations WHERE license_key=?", (key,)
            )
            (total,) = await cur.fetchone()  # type: ignore[misc]
            return (not existed), total

    async def remove_activation(self, key: str, machine_id: str) -> bool:
        async with self._connect() as conn:
            cur = await conn.execute(
                "DELETE FROM activations WHERE license_key=? AND machine_id=?",
                (key, machine_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    # --- usage events ---
    async def log_event(
        self,
        *,
        event: str,
        status: str,
        license_key: str | None,
        machine_id: str | None = None,
        product: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "INSERT INTO usage_events(license_key, machine_id, product, event, status, ip, user_agent, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (license_key, machine_id, product, event, status, ip,
                 (user_agent or "")[:200], int(time.time())),
            )
            await conn.commit()

    async def recent_events(
        self, *, limit: int = 20, license_key: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM usage_events"
        where, params = [], []
        if license_key:
            where.append("license_key=?")
            params.append(license_key)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]

    async def event_stats(
        self, license_key: str, *, since: int | None = None
    ) -> dict[str, Any]:
        params: list[Any] = [license_key]
        time_filter = ""
        if since is not None:
            time_filter = " AND created_at >= ?"
            params.append(since)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                f"SELECT event, status, COUNT(*) AS n "
                f"FROM usage_events WHERE license_key=?{time_filter} "
                f"GROUP BY event, status",
                params,
            )
            buckets = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                f"SELECT COUNT(DISTINCT machine_id) AS n FROM usage_events "
                f"WHERE license_key=? AND machine_id IS NOT NULL{time_filter}",
                params,
            )
            (distinct_machines,) = await cur.fetchone()  # type: ignore[misc]
            cur = await conn.execute(
                f"SELECT MAX(created_at) AS t FROM usage_events "
                f"WHERE license_key=?{time_filter}",
                params,
            )
            (last_seen,) = await cur.fetchone()  # type: ignore[misc]
            cur = await conn.execute(
                f"SELECT COUNT(*) FROM usage_events WHERE license_key=?{time_filter}",
                params,
            )
            (total,) = await cur.fetchone()  # type: ignore[misc]
        return {
            "total": total,
            "distinct_machines": distinct_machines,
            "last_seen": last_seen,
            "buckets": buckets,
        }

    async def purge_events(self, older_than_ts: int) -> int:
        async with self._connect() as conn:
            cur = await conn.execute(
                "DELETE FROM usage_events WHERE created_at < ?", (older_than_ts,)
            )
            await conn.commit()
            return cur.rowcount

    # --- key/value (mute toggle, dst.) ---
    async def kv_get(self, key: str) -> str | None:
        async with self._connect() as conn:
            cur = await conn.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await conn.commit()
