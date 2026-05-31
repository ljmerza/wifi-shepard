"""ADR-0007 AC-4/AC-5: DB-derived per-MAC caps through the real Actor + Database.

AC-4 proves the daily cap is restart-safe: a fresh Scanner/Actor/Backoff (no
in-memory state) still honors the cap because it is read from kick_events.
AC-5 proves a per-MAC override beats the global default.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client


def _bad(mac: str, ap_id: str = "ap1"):
    return make_client(
        mac=mac, signal=-80, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100, ap_id=ap_id
    )


@pytest.mark.asyncio
async def test_ac4_daily_cap_is_restart_safe(temp_db_path, fake_ha, caplog):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    config = build_config(dry_run=False, window_samples=1, max_kicks_per_day=3)

    db = Database(temp_db_path)
    await db.connect()
    try:
        # Run 1: three kicks fire under cap=3 and land in kick_events.
        fake1 = FakeController(clients=[_bad(bad_mac)])
        s1 = Scanner(controller=fake1, db=db, config=config, ha=fake_ha)
        for _ in range(3):
            await s1.run_once()
        assert fake1.force_reconnect_calls == [bad_mac] * 3, "first 3 kicks fire under cap=3"

        # "Restart": a brand-new Scanner/Actor/Backoff with fresh in-memory state,
        # same DB. The 4th kick must still be blocked — derived from kick_events.
        fake2 = FakeController(clients=[_bad(bad_mac)])
        s2 = Scanner(controller=fake2, db=db, config=config, ha=fake_ha)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await s2.run_once()
        assert fake2.force_reconnect_calls == [], (
            "AC-4: daily cap must hold across a restart (DB-derived, not in-memory)"
        )
        deferred = [r for r in caplog.records if r.getMessage() == "kick_deferred"]
        assert len(deferred) == 1
        assert getattr(deferred[0], "reason", None) == "per_mac_daily_cap"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac5_per_mac_cap_override_beats_global(temp_db_path, fake_ha, caplog):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    capped = "aa:aa:aa:aa:aa:aa"  # override max_kicks_per_day = 3
    normal = "bb:bb:bb:bb:bb:bb"  # global default 10
    config = build_config(
        dry_run=False,
        window_samples=1,
        max_kicks_per_day=10,
        overrides=[{"mac": capped, "max_kicks_per_day": 3}],
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        # Pre-seed 3 real kicks for each MAC.
        for mac in (capped, normal):
            for _ in range(3):
                await db.insert_kick(mac=mac, dry_run=False)

        fake = FakeController(clients=[_bad(capped), _bad(normal)])
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        assert fake.force_reconnect_calls == [normal], (
            "AC-5: capped MAC (override=3, already at 3) deferred; normal MAC (global=10) kicked"
        )
        deferred = [
            r
            for r in caplog.records
            if r.getMessage() == "kick_deferred" and getattr(r, "mac", None) == capped
        ]
        assert len(deferred) == 1
        assert getattr(deferred[0], "reason", None) == "per_mac_daily_cap"
    finally:
        await db.close()
