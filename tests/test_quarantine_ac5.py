from __future__ import annotations

import pytest

from tests.conftest import FakeController, make_client


@pytest.mark.asyncio
async def test_ac_5_quarantine_after_n_kicks(temp_db_path, fake_ha):
    from wifi_shepard.backoff import State
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
    config = build_config(dry_run=False, window_samples=1, quarantine_after_kicks=5)

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

        for _ in range(5):
            scanner.backoff.record_kick(bad_mac)

        fake.force_reconnect_calls.clear()
        fake_ha.posts.clear()

        await scanner.run_once()

        assert fake.force_reconnect_calls == [], (
            "after quarantine_after_kicks kicks, no more reconnects allowed"
        )
        quarantine_posts = [p for p in fake_ha.posts if p["severity"] == "quarantine"]
        assert len(quarantine_posts) == 1, (
            f"expected exactly one severity=quarantine HA notify, got {fake_ha.posts}"
        )
        assert quarantine_posts[0]["mac"] == bad_mac
        assert scanner.backoff.state(bad_mac) == State.QUARANTINE

        # Second cycle: still quarantined; no extra reconnects, no extra notifies.
        await scanner.run_once()
        assert fake.force_reconnect_calls == []
        quarantine_posts = [p for p in fake_ha.posts if p["severity"] == "quarantine"]
        assert len(quarantine_posts) == 1, (
            f"quarantine HA notify must fire exactly once, not on every subsequent cycle; "
            f"got {fake_ha.posts}"
        )
    finally:
        await db.close()
