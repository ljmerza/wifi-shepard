from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


@pytest.mark.asyncio
async def test_ac_3_real_kick_writes_event_notifies_and_advances_backoff(
    temp_db_path, fake_ha
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    bad = make_client(
        mac=bad_mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
    )
    fake = FakeController(clients=[bad])
    config = build_config(dry_run=False, window_samples=1)

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(
            controller=fake,
            db=db,
            poll_interval_seconds=0.001,
            config=config,
            ha=fake_ha,
        )

        await scanner.run_once()

        assert fake.force_reconnect_calls == [bad_mac], (
            f"expected exactly one force_reconnect_client({bad_mac!r}), "
            f"got {fake.force_reconnect_calls}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM kick_events WHERE mac = ? AND dry_run = 0",
                (bad_mac,),
            )
            (kick_rows,) = await cur.fetchone()
        assert kick_rows == 1, f"expected 1 kick_events row for {bad_mac}, got {kick_rows}"

        kick_posts = [p for p in fake_ha.posts if p["mac"] == bad_mac]
        assert len(kick_posts) == 1, f"expected 1 HA notification, got {len(kick_posts)}"
        assert kick_posts[0]["severity"] == "kick"

        assert scanner.backoff.kick_count(bad_mac) == 1, (
            f"backoff kick_count should advance to 1, got {scanner.backoff.kick_count(bad_mac)}"
        )
    finally:
        await db.close()
