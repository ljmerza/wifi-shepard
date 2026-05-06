from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client


@pytest.mark.asyncio
async def test_ac_2_dry_run_logs_would_kick_and_does_not_call_force_reconnect(
    temp_db_path, fake_ha, caplog
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
    config = build_config(dry_run=True, window_samples=1)

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

        assert fake.force_reconnect_calls == [], "dry_run must not call force_reconnect_client"
        assert fake_ha.posts == [], "dry_run must not send HA notifications"

        kick_logs = [
            r
            for r in caplog.records
            if r.getMessage() == "would_kick" and getattr(r, "mac", None) == bad_mac
        ]
        assert len(kick_logs) == 1, (
            f"expected 1 would_kick log for {bad_mac}, "
            f"got {[(r.getMessage(), getattr(r, 'mac', None)) for r in caplog.records]}"
        )
        assert getattr(kick_logs[0], "thresholds", None) is not None, (
            "would_kick log must carry the resolved thresholds for audit"
        )
        assert getattr(kick_logs[0], "reason", None) is not None, (
            "would_kick log must carry the reason fields"
        )
    finally:
        await db.close()
