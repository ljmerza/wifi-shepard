from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


@pytest.mark.asyncio
async def test_ac_1_scanner_writes_one_sample_per_client_per_poll(temp_db_path):
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    fake = FakeController(
        clients=[
            make_client(mac="aa:aa:aa:aa:aa:aa"),
            make_client(mac="bb:bb:bb:bb:bb:bb"),
        ]
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, poll_interval_seconds=0.001)

        await scanner.run_once()
        await scanner.run_once()

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM client_samples")
            (count,) = await cur.fetchone()
            cur_a = await conn.execute(
                "SELECT COUNT(*) FROM client_samples WHERE mac = ?",
                ("aa:aa:aa:aa:aa:aa",),
            )
            (count_a,) = await cur_a.fetchone()

        assert count == 4, f"expected 4 rows (2 clients x 2 polls), got {count}"
        assert count_a == 2, f"expected 2 rows for aa:aa:..., got {count_a}"
    finally:
        await db.close()
