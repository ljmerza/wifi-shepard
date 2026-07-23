"""ADR-0015 AC-1: forward-compatible migration adds kick_events.rationale.

Given a state.db written under the pre-ADR-0015 schema (kick_events through the
ADR-0012 `trigger` column, but no `rationale`), when Database.connect() runs
against it, then a nullable `rationale TEXT` column is added, existing rows keep
NULL, no data is lost, and a second connect is idempotent. The MySQL backend
declares the same migration (asserted statically — no live server needed).
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

# Pinned pre-ADR-0015 schema: exactly wifi_shepard.db.SCHEMA_KICK_EVENTS as it
# stood after ADR-0012 (trigger present, rationale absent). Re-declared here so
# the test setup is independent of future edits to the live SCHEMA.
PRE_0015_KICK_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    mechanism TEXT NOT NULL DEFAULT 'deauth',
    target_bssid TEXT,
    attempt_group TEXT,
    trigger TEXT NOT NULL DEFAULT 'rf'
);
"""


@pytest.mark.asyncio
async def test_ac_1_migration_adds_rationale_column_preserving_rows(temp_db_path):
    pre_ts = time.time() - 600
    pre_mac = "aa:bb:cc:dd:ee:01"

    # Phase 1: write a DB the old way (no rationale column) with a pre-existing row.
    async with aiosqlite.connect(temp_db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(PRE_0015_KICK_EVENTS_SCHEMA)
        await conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run, mechanism, trigger) "
            "VALUES (?, ?, 0, 'btm', 'rf')",
            (pre_ts, pre_mac),
        )
        await conn.commit()

    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(kick_events)")
            columns = {row[1] for row in await cur.fetchall()}
        assert "rationale" in columns, (
            f"AC-1: kick_events.rationale column missing after migration; got {sorted(columns)}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac, mechanism, rationale FROM kick_events ORDER BY ts"
            )
            rows = await cur.fetchall()
        assert len(rows) == 1, f"AC-1: pre-migration row must be preserved, got {len(rows)}"
        assert rows[0][0] == pre_mac and rows[0][1] == "btm"
        assert rows[0][2] is None, (
            f"AC-1: a pre-existing row must backfill rationale=NULL, got {rows[0][2]!r}"
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
        assert count == 1, f"AC-1 idempotency: second connect must keep exactly 1 row, got {count}"
    finally:
        await db2.close()


def test_ac_1_mysql_backend_declares_rationale_migration():
    """Both backends migrate: the MySQL adapter must declare the same column add."""
    from wifi_shepard import db_mysql

    migrated_columns = {col for col, _ddl in db_mysql._KICK_EVENTS_MIGRATIONS}
    assert "rationale" in migrated_columns, (
        "AC-1: db_mysql._KICK_EVENTS_MIGRATIONS must add the rationale column too; "
        f"got {sorted(migrated_columns)}"
    )
