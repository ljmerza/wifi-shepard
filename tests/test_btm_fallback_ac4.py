"""ADR-0003 AC-4: BTM speculative attempt + one-cycle deauth fallback.

Given the actor sent BTM under attempt_group=G for MAC M on cycle T, and on
cycle T+1 the scorer still scores M bad-state on the same ap_id, then the
actor must call force_reconnect_client(M), write a SECOND kick_events row
with mechanism='deauth_fallback' and the SAME attempt_group=G, and the
per-MAC backoff budget must NOT be incremented again — the BTM+deauth pair
counts as ONE logical kick.
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


def _bad_client(mac: str, ap_id: str = "ap1") -> object:
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
async def test_ac_4_btm_then_deauth_fallback_same_group_budget_unchanged(
    temp_db_path, fake_ha
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    config = build_config(dry_run=False, window_samples=1, kick_mechanism="auto")

    fake = FakeController(clients=[_bad_client(bad_mac, ap_id="ap1")])

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)

        # Cycle 1: BTM fires.
        await scanner.run_once()
        assert fake.btm_calls == [(bad_mac, None)], (
            f"AC-4 setup: cycle 1 must send BTM; got {fake.btm_calls}"
        )
        assert fake.force_reconnect_calls == [], (
            f"AC-4 setup: cycle 1 must NOT call deauth; got {fake.force_reconnect_calls}"
        )
        assert scanner.backoff is not None
        assert scanner.backoff.kick_count(bad_mac) == 1, (
            "AC-4 setup: cycle 1 must increment backoff once"
        )

        # Cycle 2: same client, same ap_id, still bad-state. Fallback to deauth fires.
        # The fake's clients list still references the same ap_id=ap1 client.
        await scanner.run_once()

        assert fake.force_reconnect_calls == [bad_mac], (
            f"AC-4: cycle 2 must call force_reconnect_client (deauth fallback); "
            f"got {fake.force_reconnect_calls}"
        )
        assert fake.btm_calls == [(bad_mac, None)], (
            f"AC-4: cycle 2 must NOT re-send BTM (one BTM per attempt_group); "
            f"got {fake.btm_calls}"
        )
        assert scanner.backoff.kick_count(bad_mac) == 1, (
            f"AC-4: backoff kick_count must STILL be 1 after BTM+deauth fallback "
            f"(one logical kick per attempt_group); got {scanner.backoff.kick_count(bad_mac)}"
        )

        # Two rows, same attempt_group, mechanisms 'btm' then 'deauth_fallback'.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism, attempt_group FROM kick_events "
                "WHERE mac = ? AND dry_run = 0 ORDER BY id ASC",
                (bad_mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 2, f"AC-4: expected 2 kick rows (btm + deauth_fallback), got {rows}"
        first_mech, first_group = rows[0]
        second_mech, second_group = rows[1]
        assert first_mech == "btm", f"AC-4: first row must be btm; got {first_mech!r}"
        assert second_mech == "deauth_fallback", (
            f"AC-4: second row must be deauth_fallback; got {second_mech!r}"
        )
        assert first_group is not None and first_group == second_group, (
            f"AC-4: both rows must share the same attempt_group; got {first_group!r} vs "
            f"{second_group!r}"
        )
    finally:
        await db.close()
