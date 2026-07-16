"""ADR-0010 AC-3: a client_samples table created before this ADR gains nullable
tx_bytes/rx_bytes columns on connect (existing rows backfill to NULL), and new
samples persist the counters.
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import make_client

# The pre-ADR-0010 client_samples schema (includes the ADR device-name `name`
# column but NOT the byte counters). Pinned here so the test's setup is
# independent of future changes to the live SCHEMA_CLIENT_SAMPLES.
PRE_0010_CLIENT_SAMPLES_SCHEMA = """
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
    ap_cu_total INTEGER,
    name TEXT
);
"""


@pytest.mark.asyncio
async def test_migration_adds_byte_columns_and_persists_counters(temp_db_path):
    # Phase 1: write a DB the old way with one pre-existing sample (no byte cols).
    async with aiosqlite.connect(temp_db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(PRE_0010_CLIENT_SAMPLES_SCHEMA)
        await conn.execute(
            "INSERT INTO client_samples (ts, mac, signal, tx_rate_kbps, tx_retries, "
            "wifi_tx_attempts, radio, ap_id, ap_cu_total, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1.0, "aa:bb:cc:dd:ee:01", -60, 6000, 1, 100, "ng", "ap1", 50, "old-row"),
        )
        await conn.commit()

    # Phase 2: open with the live Database; migration runs on connect.
    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(client_samples)")
            columns = {row[1] for row in await cur.fetchall()}
        assert "tx_bytes" in columns, f"tx_bytes column missing after migration; got {columns}"
        assert "rx_bytes" in columns, f"rx_bytes column missing after migration; got {columns}"

        # The pre-existing row survives, backfilled NULL for the new columns.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT tx_bytes, rx_bytes FROM client_samples WHERE mac = ?",
                ("aa:bb:cc:dd:ee:01",),
            )
            (old_tx, old_rx) = await cur.fetchone()
        assert old_tx is None and old_rx is None

        # A new sample persists the counters.
        await db.insert_sample(make_client(mac="aa:bb:cc:dd:ee:02", tx_bytes=999, rx_bytes=888))
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT tx_bytes, rx_bytes FROM client_samples WHERE mac = ?",
                ("aa:bb:cc:dd:ee:02",),
            )
            (new_tx, new_rx) = await cur.fetchone()
        assert new_tx == 999
        assert new_rx == 888
    finally:
        await db.close()

    # Idempotency: a second connect on the migrated DB must not raise or duplicate.
    db2 = Database(temp_db_path)
    await db2.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM client_samples")
            (count,) = await cur.fetchone()
        assert count == 2
    finally:
        await db2.close()
