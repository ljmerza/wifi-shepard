"""ADR-0003 AC-8: forward-compatible migration of the kick_events schema.

Given a state.db written under the ADR-0001 schema (mac, ts, dry_run only),
when the daemon's Database.connect() runs against it, then a forward-compatible
migration must add `mechanism` (DEFAULT 'deauth'), `target_bssid`, and
`attempt_group` columns without losing any pre-existing rows.
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

# ADR-0001 baseline schema — exactly what wifi_shepard.db.SCHEMA_KICK_EVENTS
# was when ADR-0001 shipped. We re-declare it here so the test's setup is
# pinned and not affected by future code changes to the live SCHEMA_KICK_EVENTS.
ADR_0001_KICK_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0
);
"""


@pytest.mark.asyncio
async def test_ac_8_migration_adds_columns_and_backfills_existing_rows(temp_db_path):
    pre_kick_ts = time.time() - 600
    pre_kick_mac = "aa:bb:cc:dd:ee:01"
    second_mac = "aa:bb:cc:dd:ee:02"

    # Phase 1: write a DB the old way (ADR-0001 schema) with two pre-existing kick rows.
    async with aiosqlite.connect(temp_db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(ADR_0001_KICK_EVENTS_SCHEMA)
        await conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, ?)",
            (pre_kick_ts, pre_kick_mac, 0),
        )
        await conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, ?)",
            (pre_kick_ts + 60, second_mac, 1),
        )
        await conn.commit()

    # Phase 2: open the same path with the live Database; migration should run on connect.
    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        # The new columns must exist on the live table.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(kick_events)")
            columns = {row[1] for row in await cur.fetchall()}
        assert "mechanism" in columns, (
            f"AC-8: kick_events.mechanism column missing after migration; got {sorted(columns)}"
        )
        assert "target_bssid" in columns, (
            f"AC-8: kick_events.target_bssid column missing after migration; got {sorted(columns)}"
        )
        assert "attempt_group" in columns, (
            f"AC-8: kick_events.attempt_group column missing after migration; got {sorted(columns)}"
        )

        # Pre-existing rows must be preserved AND backfilled with mechanism='deauth'.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac, dry_run, mechanism, target_bssid, attempt_group "
                "FROM kick_events ORDER BY ts ASC"
            )
            rows = await cur.fetchall()
        assert len(rows) == 2, f"AC-8: expected 2 pre-migration rows preserved, got {len(rows)}"
        first, second = rows
        assert first[0] == pre_kick_mac
        assert first[1] == 0
        assert first[2] == "deauth", (
            f"AC-8: pre-existing row must be backfilled with mechanism='deauth', got {first[2]!r}"
        )
        assert first[3] is None, (
            "AC-8: pre-existing row must have target_bssid=NULL "
            f"(no BTM target was recorded under the old schema); got {first[3]!r}"
        )
        assert first[4] is None, (
            "AC-8: pre-existing row must have attempt_group=NULL "
            f"(no group was recorded under the old schema); got {first[4]!r}"
        )
        assert second[0] == second_mac
        assert second[1] == 1
        assert second[2] == "deauth"
    finally:
        await db.close()

    # Idempotency: a second connect against the already-migrated DB must NOT
    # raise (sqlite ALTER TABLE on an existing column raises "duplicate column"),
    # and the row count must be unchanged. Pins the column-presence guard in
    # _migrate_kick_events; without it, every daemon restart would crash here.
    db2 = Database(temp_db_path)
    await db2.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM kick_events")
            (count_after_second_connect,) = await cur.fetchone()
        assert count_after_second_connect == 2, (
            "AC-8 idempotency: a second migration-on-connect must not lose or "
            f"duplicate rows; got {count_after_second_connect} (expected 2)"
        )
    finally:
        await db2.close()
