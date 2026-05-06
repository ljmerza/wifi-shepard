from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_ac_4_allowlisted_mac_never_kicked(temp_db_path, fake_ha, caplog, dry_run):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    listed_mac = "aa:bb:cc:dd:ee:ff"
    bad = make_client(
        mac=listed_mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
    )
    fake = FakeController(clients=[bad])
    config = build_config(dry_run=dry_run, window_samples=1, allowlist=[listed_mac])

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

        with caplog.at_level(logging.INFO, logger="wifi_shepard"):
            await scanner.run_once()

        assert fake.force_reconnect_calls == [], (
            f"allowlisted MAC must never be force-reconnected (dry_run={dry_run})"
        )
        assert fake_ha.posts == [], (
            f"allowlisted MAC must never trigger HA notify (dry_run={dry_run})"
        )

        kick_logs = [
            r
            for r in caplog.records
            if r.getMessage() == "would_kick" and getattr(r, "mac", None) == listed_mac
        ]
        assert kick_logs == [], (
            f"allowlisted MAC must never log would_kick (dry_run={dry_run}); "
            f"got {[(r.getMessage(), getattr(r, 'mac', None)) for r in kick_logs]}"
        )
    finally:
        await db.close()
