"""ADR-0004 AC-5: deauth_fallback bypasses per-AP cap (same logical kick group).

Cycle 1: BTM fires for MAC M on ap1 with kick_mechanism=auto, consuming the
sole per-AP slot (max_kicks_per_ap_per_window=1).
Cycle 2: M still bad on ap1 → deauth_fallback under the same attempt_group must
fire even though ap1 is at cap (the fallback is the SAME logical kick).
"""

from __future__ import annotations

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
async def test_ac_5_deauth_fallback_bypasses_per_ap_cap_under_same_group(temp_db_path, fake_ha):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    mac = "aa:aa:aa:aa:aa:01"
    fake = FakeController(clients=[_bad(mac, "ap1")])
    config = build_config(
        dry_run=False,
        window_samples=1,
        kick_mechanism="auto",
        safety_rails=dict(
            min_seconds_between_kicks=0,  # off — fallback isn't blocked by global
            max_kicks_per_ap_per_window=1,
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

        # Cycle 1: BTM fires; ap1 per-AP deque now [100.0]; cap=1 means ap1 is "full".
        await scanner.run_once()
        assert fake.btm_calls == [(mac, None)]
        assert fake.force_reconnect_calls == []

        # Cycle 2: M still bad on ap1 → deauth_fallback under the same attempt_group.
        # If per_ap_cap mistakenly gated the fallback, no wire call would fire.
        clock[0] = 200.0
        await scanner.run_once()

        assert fake.force_reconnect_calls == [mac], (
            f"AC-5: deauth_fallback must fire even when ap1 is at per-AP cap "
            f"(same attempt_group as the BTM stage); got {fake.force_reconnect_calls}"
        )

        # Two rows in kick_events sharing the same attempt_group.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism, attempt_group FROM kick_events "
                "WHERE mac = ? AND dry_run = 0 ORDER BY id",
                (mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 2, f"AC-5: expected 2 rows; got {rows}"
        assert rows[0][0] == "btm" and rows[1][0] == "deauth_fallback", (
            f"AC-5: cycle 1 = btm, cycle 2 = deauth_fallback; got {[r[0] for r in rows]}"
        )
        assert rows[0][1] == rows[1][1], (
            f"AC-5: both rows must share the same attempt_group; "
            f"got {rows[0][1]!r} vs {rows[1][1]!r}"
        )

        # Backoff: one logical kick → kick_count = 1.
        assert scanner.backoff is not None
        assert scanner.backoff.kick_count(mac) == 1, (
            f"AC-5: BTM+deauth_fallback is one logical kick; got "
            f"kick_count={scanner.backoff.kick_count(mac)}"
        )
    finally:
        await db.close()
