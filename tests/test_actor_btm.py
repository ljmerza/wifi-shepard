"""ADR-0003 actor-side mechanism dispatch tests.

This file groups AC-1 (deauth default), AC-2 (explicit btm), AC-3 (auto sends
BTM first), and AC-9 (dry_run logs mechanism but never calls the controller).
AC-4 (BTM-then-deauth_fallback under one attempt_group) lives in its own file
because it requires cross-cycle state.
"""

from __future__ import annotations

import uuid

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


def _bad_client(mac: str) -> object:
    return make_client(
        mac=mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
    )


@pytest.mark.asyncio
async def test_ac_1_deauth_default_calls_force_reconnect_and_records_deauth_mechanism(
    temp_db_path, fake_ha
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    fake = FakeController(clients=[_bad_client(bad_mac)])
    config = build_config(dry_run=False, window_samples=1)  # default kick_mechanism="deauth"

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        await scanner.run_once()

        assert fake.force_reconnect_calls == [bad_mac], (
            f"AC-1: deauth default must call force_reconnect_client once; "
            f"got {fake.force_reconnect_calls}"
        )
        assert fake.btm_calls == [], (
            f"AC-1: deauth default must NEVER call send_btm_request; got {fake.btm_calls}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism, target_bssid, attempt_group FROM kick_events "
                "WHERE mac = ? AND dry_run = 0",
                (bad_mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 1, f"AC-1: expected exactly one real-kick row, got {len(rows)}"
        assert rows[0][0] == "deauth", (
            f"AC-1: kick_events.mechanism must be 'deauth' for default config; got {rows[0][0]!r}"
        )
        assert rows[0][1] is None, (
            f"AC-1: deauth has no target_bssid; got {rows[0][1]!r}"
        )
        # Every kick attempt is a logical group; a deauth-only kick is its own group of one.
        # The UUID lets AC-4's fallback path link a BTM+deauth pair under the same group.
        assert rows[0][2] is not None, (
            "AC-1: kick_events.attempt_group must be set on every real kick (None means the "
            "actor isn't generating attempt_group UUIDs yet)"
        )
        uuid.UUID(rows[0][2])  # raises ValueError if it's not a valid UUID
    finally:
        await db.close()
