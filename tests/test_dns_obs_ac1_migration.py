"""ADR-0012 AC-1: forward-compatible migration for DNS observability.

Given a state.db written before ADR-0012 (kick_events without `trigger`, and no
dns_source_samples / dns_thrash_observations tables), when Database.connect()
runs, then it must add kick_events.trigger (DEFAULT 'rf', existing rows
backfilled) and create the two DNS tables — with no data loss and idempotently.
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

# Pre-ADR-0012 kick_events: has the ADR-0003 columns but NO `trigger`.
PRE_0012_KICK_EVENTS = """
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


@pytest.mark.asyncio
async def test_ac_1_migration_adds_trigger_and_dns_tables(temp_db_path):
    pre_ts = time.time() - 600
    pre_mac = "aa:bb:cc:dd:ee:01"

    # Phase 1: write a pre-ADR-0012 DB with one existing kick row.
    async with aiosqlite.connect(temp_db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(PRE_0012_KICK_EVENTS)
        await conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run, mechanism) VALUES (?, ?, 0, 'deauth')",
            (pre_ts, pre_mac),
        )
        await conn.commit()

    # Phase 2: open with the live Database — migration runs on connect.
    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(kick_events)")
            kick_cols = {row[1] for row in await cur.fetchall()}
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in await cur.fetchall()}
            cur = await conn.execute("SELECT mac, trigger FROM kick_events")
            rows = await cur.fetchall()

        assert "trigger" in kick_cols, (
            f"AC-1: kick_events.trigger missing after migration; got {sorted(kick_cols)}"
        )
        assert "dns_source_samples" in tables, "AC-1: dns_source_samples table must be created"
        assert "dns_thrash_observations" in tables, (
            "AC-1: dns_thrash_observations table must be created"
        )
        assert len(rows) == 1, f"AC-1: the pre-existing kick row must be preserved; got {rows}"
        assert rows[0][0] == pre_mac
        assert rows[0][1] == "rf", (
            f"AC-1: pre-existing kick must backfill trigger='rf'; got {rows[0][1]!r}"
        )
    finally:
        await db.close()

    # Idempotency: a second connect must not raise (duplicate-column) or lose rows.
    db2 = Database(temp_db_path)
    await db2.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM kick_events")
            (count,) = await cur.fetchone()
        assert count == 1, f"AC-1 idempotency: row count must be unchanged; got {count}"
    finally:
        await db2.close()
