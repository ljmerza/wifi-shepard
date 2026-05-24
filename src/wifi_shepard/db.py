from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite


@runtime_checkable
class Store(Protocol):
    """Persistence surface the scan pipeline depends on.

    Captures exactly what Scanner/Actor need from storage (sample + kick
    writes), so they depend on this abstraction rather than the concrete
    ``Database``. The connect()/close() lifecycle is the composition root's
    concern (main.Daemon) and is intentionally not part of this surface.
    """

    async def insert_sample(self, client: Any) -> None: ...

    async def insert_kick(
        self,
        *,
        mac: str,
        dry_run: bool,
        mechanism: str = "deauth",
        target_bssid: str | None = None,
        attempt_group: str | None = None,
    ) -> None: ...


SCHEMA_CLIENT_SAMPLES = """
CREATE TABLE IF NOT EXISTS client_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    signal INTEGER,
    tx_rate_kbps INTEGER,
    tx_retries INTEGER,
    wifi_tx_attempts INTEGER,
    radio TEXT,
    ap_id TEXT,
    ap_cu_total INTEGER
);
"""

SCHEMA_KICK_EVENTS = """
CREATE TABLE IF NOT EXISTS kick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    mechanism TEXT NOT NULL DEFAULT 'deauth',
    target_bssid TEXT,
    attempt_group TEXT
);
"""

# Forward-compatible migration: a kick_events table created under ADR-0001 has
# only (id, ts, mac, dry_run). Each ALTER TABLE adds one missing column,
# backfilling existing rows with the default. ADR-0003 AC-8.
_KICK_EVENTS_MIGRATIONS = (
    ("mechanism", "ALTER TABLE kick_events ADD COLUMN mechanism TEXT NOT NULL DEFAULT 'deauth'"),
    ("target_bssid", "ALTER TABLE kick_events ADD COLUMN target_bssid TEXT"),
    ("attempt_group", "ALTER TABLE kick_events ADD COLUMN attempt_group TEXT"),
)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self.closed = False

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(SCHEMA_CLIENT_SAMPLES)
        await self._conn.execute(SCHEMA_KICK_EVENTS)
        await self._migrate_kick_events()
        await self._conn.commit()

    async def _migrate_kick_events(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database._migrate_kick_events called before connect()")
        cur = await self._conn.execute("PRAGMA table_info(kick_events)")
        existing = {row[1] for row in await cur.fetchall()}
        for column, ddl in _KICK_EVENTS_MIGRATIONS:
            if column not in existing:
                await self._conn.execute(ddl)

    async def insert_sample(self, client: Any) -> None:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before insert_sample()")
        await self._conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                client.mac,
                client.signal,
                client.tx_rate_kbps,
                client.tx_retries,
                client.wifi_tx_attempts,
                client.radio,
                client.ap_id,
                client.ap_cu_total,
            ),
        )
        await self._conn.commit()

    async def insert_kick(
        self,
        *,
        mac: str,
        dry_run: bool,
        mechanism: str = "deauth",
        target_bssid: str | None = None,
        attempt_group: str | None = None,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before insert_kick()")
        await self._conn.execute(
            "INSERT INTO kick_events "
            "(ts, mac, dry_run, mechanism, target_bssid, attempt_group) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), mac, 1 if dry_run else 0, mechanism, target_bssid, attempt_group),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self.closed = True
