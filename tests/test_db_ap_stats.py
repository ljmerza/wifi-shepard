"""AP-stats persistence: the `name`-column migration on client_samples and the
insert_ap_stats helper that writes ap_samples + ap_radio_samples per poll."""

from __future__ import annotations

import time

import aiosqlite
import pytest

# client_samples as it existed before the UI device-name feature — no `name`
# column. Re-declared here so the migration test's setup is pinned and not
# affected by future edits to the live SCHEMA_CLIENT_SAMPLES.
PRE_NAME_CLIENT_SAMPLES_SCHEMA = """
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


@pytest.mark.asyncio
async def test_client_samples_name_migration_adds_column_and_preserves_rows(temp_db_path):
    pre_ts = time.time() - 600

    # Phase 1: write a DB the old way (no `name` column) with one pre-existing row.
    async with aiosqlite.connect(temp_db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(PRE_NAME_CLIENT_SAMPLES_SCHEMA)
        await conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, wifi_tx_attempts, "
            " radio, ap_id, ap_cu_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pre_ts, "aa:bb:cc:dd:ee:01", -70, 6000, 0, 100, "ng", "ap1", 70),
        )
        await conn.commit()

    # Phase 2: open with the live Database; migration must add `name` on connect.
    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(client_samples)")
            columns = {row[1] for row in await cur.fetchall()}
            assert "name" in columns, f"name column missing after migration; got {sorted(columns)}"

            cur = await conn.execute("SELECT mac, name FROM client_samples")
            rows = await cur.fetchall()
        assert len(rows) == 1, "pre-existing row must be preserved"
        assert rows[0][0] == "aa:bb:cc:dd:ee:01"
        assert rows[0][1] is None, "pre-existing row must backfill name=NULL"
    finally:
        await db.close()

    # Idempotency: a second connect must not raise (duplicate-column guard) or
    # lose/duplicate rows.
    db2 = Database(temp_db_path)
    await db2.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM client_samples")
            (count,) = await cur.fetchone()
        assert count == 1
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_insert_ap_stats_writes_ap_and_paired_radio_rows(temp_db_path):
    from wifi_shepard.controllers.base import APStats, RadioStats
    from wifi_shepard.db import Database

    ap = APStats(
        id="ff:ee:dd:cc:bb:aa",
        name="Front Porch",
        mac="ff:ee:dd:cc:bb:aa",
        cpu_pct=6.4,
        mem_pct=42.1,
        radios=(
            RadioStats(radio="ng", cu_total=72, bssid="b1", channel=6),
            RadioStats(radio="na", cu_total=35, bssid="b2", channel=36),
        ),
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        await db.insert_ap_stats(ap)
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT ts, ap_id, name, mac, cpu_pct, mem_pct FROM ap_samples"
            )
            ap_rows = await cur.fetchall()
            cur = await conn.execute(
                "SELECT ts, ap_id, radio, channel, cu_total FROM ap_radio_samples ORDER BY radio"
            )
            radio_rows = await cur.fetchall()

        assert len(ap_rows) == 1
        ap_ts, ap_id, name, mac, cpu, mem = ap_rows[0]
        assert (ap_id, name, mac, cpu, mem) == (
            "ff:ee:dd:cc:bb:aa",
            "Front Porch",
            "ff:ee:dd:cc:bb:aa",
            pytest.approx(6.4),
            pytest.approx(42.1),
        )

        assert len(radio_rows) == 2, "one ap_radio_samples row per radio"
        # All radio rows share the AP row's ts so the UI can pair them per poll.
        assert {r[0] for r in radio_rows} == {ap_ts}
        by_radio = {r[2]: r for r in radio_rows}
        assert (by_radio["ng"][3], by_radio["ng"][4]) == (6, 72)
        assert (by_radio["na"][3], by_radio["na"][4]) == (36, 35)
    finally:
        await db.close()
