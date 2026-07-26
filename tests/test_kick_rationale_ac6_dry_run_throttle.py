"""ADR-0015 AC-6: dry-run kicks are persisted, throttled to the first cooldown.

In dry_run mode the actor writes a dry_run=1 row carrying the rationale, but at
most one row per MAC per first-cooldown interval — a MAC flagged on consecutive
cycles inside that interval yields exactly one row; a fresh MAC writes its own.
"""

from __future__ import annotations

import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.resolution import resolve_thresholds


async def _dry_run_count(conn, mac):
    cur = await conn.execute(
        "SELECT COUNT(*) FROM kick_events WHERE mac = ? AND dry_run = 1", (mac,)
    )
    (n,) = await cur.fetchone()
    return n


@pytest.mark.asyncio
async def test_ac_6_dry_run_rows_written_and_throttled(temp_db_path):
    import aiosqlite

    mac = "aa:bb:cc:dd:ee:06"
    other = "aa:bb:cc:dd:ee:16"
    # First cooldown is 300s — the throttle interval for dry-run rows.
    config = build_config(
        dry_run=True,
        cooldowns_seconds=(300, 1800),
        signal_dbm_max=-70,
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
        window_samples=1,
    )
    clock = {"t": 1000.0}
    client = make_client(
        mac=mac, signal=-85, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100
    )
    ctx = resolve_thresholds(mac, config)

    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(
            config=config, controller=FakeController(), db=db, wall_now_fn=lambda: clock["t"]
        )

        await actor.handle(client, ctx)  # t=1000 -> first row
        async with aiosqlite.connect(temp_db_path) as conn:
            assert await _dry_run_count(conn, mac) == 1, (
                "AC-6: dry_run must persist a would-kick row"
            )

        clock["t"] = 1000 + 299
        await actor.handle(client, ctx)  # inside the 300s window -> throttled
        async with aiosqlite.connect(temp_db_path) as conn:
            assert await _dry_run_count(conn, mac) == 1, (
                "AC-6: a second flag inside the first-cooldown interval must be throttled"
            )

        clock["t"] = 1000 + 300
        await actor.handle(client, ctx)  # interval elapsed -> second row
        async with aiosqlite.connect(temp_db_path) as conn:
            assert await _dry_run_count(conn, mac) == 2, (
                "AC-6: once the interval elapses a new would-kick row must be written"
            )

        # The throttle is per-MAC: a different MAC writes immediately.
        await actor.handle(
            make_client(
                mac=other, signal=-85, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100
            ),
            resolve_thresholds(other, config),
        )
        async with aiosqlite.connect(temp_db_path) as conn:
            assert await _dry_run_count(conn, other) == 1, (
                "AC-6: the throttle must be per-MAC, not global"
            )
    finally:
        await db.close()
