"""Regression: _pending_btm must be cleared after a successful BTM kick.

When BTM succeeds (client roams off the original AP), the actor's in-memory
_pending_btm[mac] entry should be cleaned up. Otherwise, if the client later
returns to the original AP and goes bad-state again, the actor would fire
deauth_fallback under the original (stale) attempt_group — corrupting the
audit trail and bypassing the backoff budget for that fresh kick.

Scenario:
  Cycle 1: client bad-state on ap1, kick_mechanism=auto -> BTM fires.
  Cycle 2: client roamed to ap2, healthy -> kick_succeeded; _pending_btm cleared.
  Cycle 3: client returns to ap1 bad-state -> must be a FRESH BTM under a
           NEW attempt_group, NOT deauth_fallback under the old group.
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


def _healthy_client(mac: str, ap_id: str) -> object:
    return make_client(
        mac=mac,
        signal=-50,
        tx_rate_kbps=300_000,
        tx_retries=1,
        wifi_tx_attempts=100,
        radio="ng",
        ap_id=ap_id,
    )


@pytest.mark.asyncio
async def test_pending_btm_cleared_after_successful_roam(temp_db_path, fake_ha):
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

        # Cycle 1: BTM fires at ap1.
        await scanner.run_once()
        assert fake.btm_calls == [(bad_mac, None)]
        assert fake.force_reconnect_calls == []

        # Cycle 2: client roamed to ap2 and is healthy. kick_succeeded fires;
        # _pending_btm[mac] must be cleared as a side-effect.
        fake.clients = [_healthy_client(bad_mac, ap_id="ap2")]
        await scanner.run_once()
        assert scanner.actor is not None
        assert bad_mac not in scanner.actor._pending_btm, (
            "after kick_succeeded, _pending_btm must be cleared so a future "
            "bad-state at the original ap_id does not fire stale deauth_fallback"
        )

        # Cycle 3: client is back at ap1 in bad-state. This must be a FRESH BTM
        # (new attempt_group), NOT deauth_fallback under the cycle-1 group.
        fake.clients = [_bad_client(bad_mac, ap_id="ap1")]
        await scanner.run_once()

        # Two BTM calls total: cycle 1 and cycle 3.
        assert fake.btm_calls == [(bad_mac, None), (bad_mac, None)], (
            f"cycle 3 must send a fresh BTM (not deauth_fallback); btm_calls={fake.btm_calls}"
        )
        # No deauth ever fired — only BTM, twice.
        assert fake.force_reconnect_calls == [], (
            f"cycle 3 must not fire deauth_fallback under a stale group; "
            f"force_reconnect_calls={fake.force_reconnect_calls}"
        )

        # Backoff incremented twice: once per logical kick.
        assert scanner.backoff is not None
        assert scanner.backoff.kick_count(bad_mac) == 2, (
            f"each logical kick increments backoff once; got "
            f"kick_count={scanner.backoff.kick_count(bad_mac)}"
        )

        # Two kick_events rows, each with mechanism='btm', and DIFFERENT
        # attempt_group UUIDs.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism, attempt_group FROM kick_events "
                "WHERE mac = ? AND dry_run = 0 ORDER BY id ASC",
                (bad_mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 2, f"expected 2 kick rows; got {rows}"
        assert rows[0][0] == "btm" and rows[1][0] == "btm", (
            f"both rows must be 'btm' (no spurious deauth_fallback); got {rows}"
        )
        assert rows[0][1] != rows[1][1], (
            f"each fresh kick must have its own attempt_group; got {rows[0][1]!r} == {rows[1][1]!r}"
        )
    finally:
        await db.close()
