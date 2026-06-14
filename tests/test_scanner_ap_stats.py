"""The scan loop persists AP-level health (identity + CPU/mem + per-radio CU)
once per poll, alongside the per-client samples."""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


@pytest.mark.asyncio
async def test_scanner_persists_ap_stats_each_poll(temp_db_path):
    from wifi_shepard.controllers.base import APStats, RadioStats
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    ap = APStats(
        id="ff:ee:dd:cc:bb:aa",
        name="Front Porch",
        mac="ff:ee:dd:cc:bb:aa",
        cpu_pct=6.0,
        mem_pct=42.0,
        radios=(
            RadioStats(radio="ng", cu_total=72, bssid="b1", channel=6),
            RadioStats(radio="na", cu_total=35, bssid="b2", channel=36),
        ),
    )
    fake = FakeController(clients=[make_client(mac="aa:aa:aa:aa:aa:aa")], ap_stats=[ap])

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, poll_interval_seconds=0.001)

        await scanner.run_once()
        await scanner.run_once()

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM ap_samples")
            (ap_count,) = await cur.fetchone()
            cur = await conn.execute("SELECT COUNT(*) FROM ap_radio_samples")
            (radio_count,) = await cur.fetchone()

        assert ap_count == 2, "one ap_samples row per AP per poll (1 AP x 2 polls)"
        assert radio_count == 4, "one ap_radio_samples row per radio per poll (2 radios x 2 polls)"
    finally:
        await db.close()
