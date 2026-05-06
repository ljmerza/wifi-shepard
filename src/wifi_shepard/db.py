from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

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


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self.closed = False

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(SCHEMA_CLIENT_SAMPLES)
        await self._conn.commit()

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

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self.closed = True
