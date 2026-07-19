"""Indexes for the UI's hot query paths + the 30-day client_samples retention.

Two concerns, both added to make the read-only UI load fast without letting
client_samples grow unbounded:

1. connect() creates a (mac COLLATE NOCASE, ts) index on client_samples and an
   (ap_id, radio, ts) index on ap_radio_samples. The NOCASE collation is load-
   bearing: the UI filters with `WHERE mac = ? COLLATE NOCASE`, and a BINARY
   index is silently ignored for that predicate (full scan).
2. prune_client_samples() deletes rows older than 30 days, throttled to once
   per hour so the ts-scan cost stays negligible.
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

from wifi_shepard.db import (
    _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS,
    _CLIENT_SAMPLES_RETENTION_SECONDS,
    Database,
)


@pytest.mark.asyncio
async def test_connect_creates_expected_indexes(temp_db_path):
    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'ix_%'"
            )
            names = {row[0] for row in await cur.fetchall()}
    finally:
        await db.close()

    assert "ix_client_samples_mac_ts" in names
    assert "ix_ap_radio_samples_ap_radio_ts" in names


@pytest.mark.asyncio
async def test_history_query_uses_index_not_full_scan(temp_db_path):
    # The exact predicate the UI uses (mac = ? COLLATE NOCASE ORDER BY ts DESC).
    # The plan must SEARCH via our index, not SCAN the whole table — the whole
    # point of declaring the index with matching NOCASE collation.
    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT ts, signal FROM client_samples "
                "WHERE mac = 'AA:BB:CC:DD:EE:FF' COLLATE NOCASE ORDER BY ts DESC LIMIT 500"
            )
            plan = " ".join(str(row[3]) for row in await cur.fetchall())
    finally:
        await db.close()

    assert "ix_client_samples_mac_ts" in plan, plan
    assert "SCAN client_samples" not in plan, plan


async def _insert_sample_at(conn: aiosqlite.Connection, *, ts: float, mac: str) -> None:
    await conn.execute(
        "INSERT INTO client_samples (ts, mac) VALUES (?, ?)",
        (ts, mac),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_prune_deletes_only_rows_older_than_retention(temp_db_path):
    now = 1_700_000_000.0
    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            # One row just past the window, one just inside it.
            await _insert_sample_at(
                conn, ts=now - _CLIENT_SAMPLES_RETENTION_SECONDS - 60, mac="old"
            )
            await _insert_sample_at(
                conn, ts=now - _CLIENT_SAMPLES_RETENTION_SECONDS + 60, mac="new"
            )

        deleted = await db.prune_client_samples(now=now)
        assert deleted == 1

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT mac FROM client_samples")
            remaining = {row[0] for row in await cur.fetchall()}
        assert remaining == {"new"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_is_throttled_within_the_interval(temp_db_path):
    now = 1_700_000_000.0
    db = Database(temp_db_path)
    await db.connect()
    try:
        async with aiosqlite.connect(temp_db_path) as conn:
            await _insert_sample_at(
                conn, ts=now - _CLIENT_SAMPLES_RETENTION_SECONDS - 60, mac="old1"
            )

        first = await db.prune_client_samples(now=now)
        assert first == 1

        # A second stale row appears, but the next prune is within the throttle
        # window, so it must be a no-op (0 deleted, row still present).
        async with aiosqlite.connect(temp_db_path) as conn:
            await _insert_sample_at(
                conn, ts=now - _CLIENT_SAMPLES_RETENTION_SECONDS - 60, mac="old2"
            )
        throttled = await db.prune_client_samples(
            now=now + _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS - 1
        )
        assert throttled == 0

        # Past the interval it runs again and reaps the second stale row.
        after = await db.prune_client_samples(now=now + _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS + 1)
        assert after == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scanner_run_once_prunes_stale_rows(temp_db_path):
    from tests.conftest import FakeController, make_client
    from wifi_shepard.scanner import Scanner

    fake = FakeController(clients=[make_client(mac="aa:aa:aa:aa:aa:aa")])
    db = Database(temp_db_path)
    await db.connect()
    try:
        # A stale row predating the retention window, written directly.
        async with aiosqlite.connect(temp_db_path) as conn:
            await _insert_sample_at(
                conn,
                ts=time.time() - _CLIENT_SAMPLES_RETENTION_SECONDS - 3600,
                mac="stale:mac",
            )

        scanner = Scanner(controller=fake, db=db, poll_interval_seconds=0.001)
        await scanner.run_once()

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM client_samples WHERE mac = 'stale:mac'")
            (stale_count,) = await cur.fetchone()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM client_samples WHERE mac = 'aa:aa:aa:aa:aa:aa'"
            )
            (fresh_count,) = await cur.fetchone()

        assert stale_count == 0, "run_once should prune rows older than the retention window"
        assert fresh_count == 1, "the just-written sample must survive"
    finally:
        await db.close()
