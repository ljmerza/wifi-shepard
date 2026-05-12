"""ADR-0004 AC-1: default config (no safety_rails block) preserves ADR-0003 behavior.

With both rate-limit knobs at 0 (off), the actor must produce identical wire
sequences, kick_events rows, and backoff increments as it did before this
feature landed — and never emit a kick_deferred log line.
"""

from __future__ import annotations

import logging

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


def _bad(mac: str, ap_id: str = "ap1") -> object:
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
async def test_ac_1_default_no_safety_rails_block_matches_baseline(temp_db_path, fake_ha, caplog):
    """N=3 bad-state MACs in one cycle, kick_mechanism=deauth, no safety_rails.
    All 3 must kick; backoff = 1 each; kick_events rows = 3; no kick_deferred."""
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    macs = ["aa:aa:aa:aa:aa:01", "aa:aa:aa:aa:aa:02", "aa:aa:aa:aa:aa:03"]
    fake = FakeController(clients=[_bad(m) for m in macs])
    config = build_config(dry_run=False, window_samples=1)  # no safety_rails kwargs → off

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        # All 3 deauth wire calls fired; no BTM (kick_mechanism=deauth default).
        assert fake.force_reconnect_calls == macs, (
            f"AC-1: default kick_mechanism + no safety_rails must fire all N kicks; "
            f"got {fake.force_reconnect_calls}"
        )
        assert fake.btm_calls == [], f"AC-1: deauth default must not call BTM; got {fake.btm_calls}"

        # No kick_deferred log lines.
        deferred = [r for r in caplog.records if r.getMessage() == "kick_deferred"]
        assert deferred == [], (
            f"AC-1: default-off safety_rails must not produce kick_deferred lines; got {deferred}"
        )

        # kick_events row count == N.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM kick_events WHERE dry_run = 0",
            )
            (count,) = await cur.fetchone()
        assert count == len(macs), (
            f"AC-1: kick_events row count must equal N; got {count} vs {len(macs)}"
        )

        # Per-MAC backoff incremented exactly once each.
        assert scanner.backoff is not None
        for m in macs:
            assert scanner.backoff.kick_count(m) == 1, (
                f"AC-1: backoff kick_count for {m} must be 1; got {scanner.backoff.kick_count(m)}"
            )
    finally:
        await db.close()
