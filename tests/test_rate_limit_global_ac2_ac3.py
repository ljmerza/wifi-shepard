"""ADR-0004 AC-2 + AC-3: global single-flight blocks the second kick, then releases.

AC-2 — Two bad-state MACs in one cycle with min_seconds_between_kicks=30:
  first fires, second is deferred with reason=global_rate_limit; no wire call,
  no kick_events row, no backoff increment for the second.

AC-3 — Same scenario, clock advanced past the window; the deferred MAC kicks
  normally on the next cycle.
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
async def test_ac_2_second_kick_in_same_cycle_is_deferred_global_rate_limit(
    temp_db_path, fake_ha, caplog
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    mac_a = "aa:aa:aa:aa:aa:01"
    mac_b = "bb:bb:bb:bb:bb:02"
    # Different APs so the per-AP cap doesn't interfere; only global single-flight matters.
    fake = FakeController(clients=[_bad(mac_a, "ap1"), _bad(mac_b, "ap2")])
    config = build_config(
        dry_run=False,
        window_samples=1,
        safety_rails=dict(min_seconds_between_kicks=30),
    )

    # Fixed clock so both kicks see the same `now`. The actor's now_fn returns
    # this value on every call within one cycle.
    clock = [100.0]

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        assert scanner.actor is not None
        scanner.actor.now_fn = lambda: clock[0]

        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        # Exactly one wire call — the first MAC.
        assert fake.force_reconnect_calls == [mac_a], (
            f"AC-2: only the first kick must fire when global single-flight is set; "
            f"got force_reconnect_calls={fake.force_reconnect_calls}"
        )

        # kick_deferred log line for the second MAC.
        deferred = [r for r in caplog.records if r.getMessage() == "kick_deferred"]
        assert len(deferred) == 1, (
            f"AC-2: expected exactly one kick_deferred log line; got {len(deferred)}"
        )
        rec = deferred[0]
        assert getattr(rec, "mac", None) == mac_b, (
            f"AC-2: kick_deferred must name the blocked MAC; got mac={getattr(rec, 'mac', None)!r}"
        )
        assert getattr(rec, "reason", None) == "global_rate_limit", (
            f"AC-2: kick_deferred.reason must be 'global_rate_limit'; "
            f"got {getattr(rec, 'reason', None)!r}"
        )
        retry = getattr(rec, "retry_after_seconds", None)
        assert retry is not None and retry > 0, (
            f"AC-2: kick_deferred must include retry_after_seconds; got {retry!r}"
        )

        # kick_events: only one row written (for mac_a).
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac FROM kick_events WHERE dry_run = 0 ORDER BY id",
            )
            rows = await cur.fetchall()
        assert [r[0] for r in rows] == [mac_a], (
            f"AC-2: kick_events must only contain the first kick; got {rows}"
        )

        # Backoff: mac_a +1, mac_b unchanged at 0.
        assert scanner.backoff is not None
        assert scanner.backoff.kick_count(mac_a) == 1, (
            f"AC-2: first MAC's backoff incremented; got {scanner.backoff.kick_count(mac_a)}"
        )
        assert scanner.backoff.kick_count(mac_b) == 0, (
            f"AC-2: deferred MAC's backoff must NOT increment (kick didn't happen); "
            f"got {scanner.backoff.kick_count(mac_b)}"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_3_deferred_mac_kicks_normally_after_window_elapses(temp_db_path, fake_ha):
    """Set up AC-2 state, advance the clock past the window, run another cycle.
    The still-bad-state second MAC must kick normally."""
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    mac_a = "aa:aa:aa:aa:aa:01"
    mac_b = "bb:bb:bb:bb:bb:02"
    fake = FakeController(clients=[_bad(mac_a, "ap1"), _bad(mac_b, "ap2")])
    config = build_config(
        dry_run=False,
        window_samples=1,
        safety_rails=dict(min_seconds_between_kicks=30),
    )

    clock = [100.0]

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        assert scanner.actor is not None
        scanner.actor.now_fn = lambda: clock[0]

        # Cycle 1: a kicks, b deferred.
        await scanner.run_once()
        assert fake.force_reconnect_calls == [mac_a]

        # Advance past the window. Both clients still bad-state.
        clock[0] = 200.0

        # Cycle 2: b should now kick. a is also still bad — but cycle ordering means
        # a will fire first AGAIN at t=200 (consuming the single-flight slot), and
        # b will defer AGAIN at t=200. To unambiguously test AC-3, drop a from the
        # input on cycle 2 so b is the only candidate.
        fake.clients = [_bad(mac_b, "ap2")]
        await scanner.run_once()

        assert fake.force_reconnect_calls == [mac_a, mac_b], (
            f"AC-3: after window elapses, deferred MAC must kick; "
            f"got force_reconnect_calls={fake.force_reconnect_calls}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac FROM kick_events WHERE dry_run = 0 ORDER BY id",
            )
            rows = await cur.fetchall()
        assert [r[0] for r in rows] == [mac_a, mac_b]
    finally:
        await db.close()
