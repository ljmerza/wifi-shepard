"""ADR-0004 AC-4: per-AP cap blocks the third kick against one AP in one window.

3 bad-state MACs on ap1, max_kicks_per_ap_per_window=2 (global rate limit off so
it doesn't shadow per_ap_cap): first two kick, third logs kick_deferred with
reason=per_ap_cap and ap_id=ap1; backoff for the third MAC stays at 0.
"""

from __future__ import annotations

import logging

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


def _bad(mac: str, ap_id: str) -> object:
    return make_client(
        mac=mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
        ap_id=ap_id,
    )


@pytest.mark.asyncio
async def test_ac_4_third_kick_against_same_ap_is_deferred_per_ap_cap(
    temp_db_path, fake_ha, caplog
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    macs = ["aa:aa:aa:aa:aa:01", "bb:bb:bb:bb:bb:02", "cc:cc:cc:cc:cc:03"]
    fake = FakeController(clients=[_bad(m, "ap1") for m in macs])
    config = build_config(
        dry_run=False,
        window_samples=1,
        safety_rails=dict(
            min_seconds_between_kicks=0,  # global off so per_ap_cap is the only gate
            max_kicks_per_ap_per_window=2,
            per_ap_window_seconds=600,
        ),
    )

    clock = [100.0]

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        assert scanner.actor is not None
        scanner.actor.now_fn = lambda: clock[0]

        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        # First 2 fired; third deferred.
        assert fake.force_reconnect_calls == macs[:2], (
            f"AC-4: only the first 2 kicks must fire against ap1 with cap=2; "
            f"got force_reconnect_calls={fake.force_reconnect_calls}"
        )

        deferred = [r for r in caplog.records if r.getMessage() == "kick_deferred"]
        assert len(deferred) == 1, (
            f"AC-4: expected exactly one kick_deferred log; got {len(deferred)}"
        )
        rec = deferred[0]
        assert getattr(rec, "mac", None) == macs[2], (
            f"AC-4: deferred MAC must be the third; got mac={getattr(rec, 'mac', None)!r}"
        )
        assert getattr(rec, "reason", None) == "per_ap_cap", (
            f"AC-4: reason must be 'per_ap_cap'; got {getattr(rec, 'reason', None)!r}"
        )
        assert getattr(rec, "ap_id", None) == "ap1", (
            f"AC-4: kick_deferred.ap_id must name the capped AP; "
            f"got {getattr(rec, 'ap_id', None)!r}"
        )

        # kick_events: 2 rows.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac FROM kick_events WHERE dry_run = 0 ORDER BY id",
            )
            rows = await cur.fetchall()
        assert [r[0] for r in rows] == macs[:2]

        # Backoff: third MAC stays at 0.
        assert scanner.backoff is not None
        assert scanner.backoff.kick_count(macs[2]) == 0, (
            f"AC-4: deferred MAC's backoff must NOT increment; "
            f"got {scanner.backoff.kick_count(macs[2])}"
        )
    finally:
        await db.close()
